"""Compute implied quarter-end prices (quarter_prices) and fundamental/dividend metrics (stock_metrics).

AGENTS.md rules:
- Compute payout ratios on a same-fiscal-year basis (DPS / EPS, DividendsPaid / NetIncome, DividendsPaid / FCF).
- Derive quarter-end prices from ETF holdings (value_usd / shares).
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from src.env import RAW_DIR
from src.fundamentals import (
    extract_annual_financials,
    get_cik_chain,
    load_cik_overrides,
    merge_company_facts,
)


def _effective_dps(row: sqlite3.Row | dict[str, Any]) -> tuple[float | None, bool]:
    """Return effective DPS and fallback flag from an annual row (declared > paid > dividends_paid/shares)."""
    if row["dps_declared"] is not None:
        return float(row["dps_declared"]), False
    if row["dps_paid"] is not None:
        return float(row["dps_paid"]), False
    div_paid = row["dividends_paid"]
    sh = row["shares_diluted"] or row["shares_outstanding"]
    if div_paid is not None and sh is not None and sh > 0:
        return round(float(div_paid) / float(sh), 4), True
    return None, False


def _calc_cagr(start_val: float | None, end_val: float | None, years: int) -> float | None:
    if start_val is None or end_val is None or years <= 0:
        return None
    if start_val <= 0 or end_val <= 0:
        return None
    return (end_val / start_val) ** (1.0 / years) - 1.0


def rebuild_quarter_prices(conn: sqlite3.Connection) -> int:
    """Rebuild quarter_prices from etf_holdings (value_usd / shares)."""
    conn.execute("DELETE FROM quarter_prices")
    # When multiple ETFs hold a security on the same date, use the ETF with the largest position value
    rows = conn.execute(
        """
        SELECT security_id, as_of, etf_id, value_usd, shares
        FROM etf_holdings
        WHERE shares > 0 AND value_usd > 0
        ORDER BY security_id, as_of, value_usd DESC
        """
    ).fetchall()

    best: dict[tuple[str, str], tuple[float, str]] = {}
    for r in rows:
        key = (r["security_id"], r["as_of"])
        if key not in best:
            price = float(r["value_usd"]) / float(r["shares"])
            best[key] = (round(price, 4), r["etf_id"])

    conn.executemany(
        """
        INSERT INTO quarter_prices (security_id, as_of, price_usd, source_etf)
        VALUES (?, ?, ?, ?)
        """,
        [(sec_id, as_of, price, etf_id) for (sec_id, as_of), (price, etf_id) in sorted(best.items())],
    )
    conn.commit()
    return len(best)


def _compute_dividend_streak(
    annual_rows: list[sqlite3.Row],
) -> tuple[int | None, int | None, bool]:
    """Return consecutive non-cut dividend years, open-start flag, and whether fallback DPS was used."""
    series: list[tuple[int, float, bool]] = []
    for row in annual_rows:
        dps, is_est = _effective_dps(row)
        if dps is not None:
            year = int(str(row["fy_end"])[:4])
            series.append((year, dps, is_est))

    if not series:
        return None, None, False

    used_est = any(x[2] for x in series)
    latest_year, latest_dps, _ = series[-1]
    if latest_dps <= 0:
        return 0, 0, used_est

    streak = 1
    reached_start = True
    cur_dps = latest_dps

    for idx in range(len(series) - 2, -1, -1):
        _yr, prev_dps, _ = series[idx]
        if prev_dps <= 0:
            reached_start = False
            break
        # Allow a 1.5% rounding/fx tolerance when checking for non-cut dividends
        if cur_dps >= prev_dps * 0.985:
            streak += 1
            cur_dps = prev_dps
        else:
            reached_start = False
            break

    # Mark streak as open-ended (e.g. 15+ yrs) if it runs unbroken back to the earliest fiscal year
    is_open = 1 if (reached_start and len(series) >= 2) else 0
    return streak, is_open, used_est


def _find_value_n_years_ago(
    series: list[tuple[int, float]],
    latest_year: int,
    n_years: int,
) -> tuple[float | None, int]:
    """Find the value n_years prior to latest_year (allowing +/-1 yr) and return (val, actual_span)."""
    target = latest_year - n_years
    by_yr = {yr: val for yr, val in series}
    if target in by_yr:
        return by_yr[target], n_years
    # Absorb 1-year boundary shifts from 52/53-week fiscal calendars
    for delta in (-1, 1):
        if (target + delta) in by_yr:
            span = latest_year - (target + delta)
            if span >= max(1, n_years - 1):
                return by_yr[target + delta], span
    return None, 0


def rebuild_stock_metrics(conn: sqlite3.Connection) -> int:
    """Recompute and store stock_metrics for all securities."""
    conn.execute("DELETE FROM stock_metrics")

    sec_ids = [
        r["security_id"]
        for r in conn.execute("SELECT security_id FROM securities ORDER BY security_id").fetchall()
    ]

    # Latest quarter-end price map
    latest_prices: dict[str, tuple[str, float]] = {}
    for r in conn.execute(
        """
        SELECT q.security_id, q.as_of, q.price_usd
        FROM quarter_prices q
        INNER JOIN (
            SELECT security_id, MAX(as_of) AS max_as_of
            FROM quarter_prices
            GROUP BY security_id
        ) m ON q.security_id = m.security_id AND q.as_of = m.max_as_of
        """
    ).fetchall():
        latest_prices[r["security_id"]] = (r["as_of"], float(r["price_usd"]))

    ttm_map = {
        r["security_id"]: r
        for r in conn.execute("SELECT * FROM financials_ttm").fetchall()
    }

    annual_by_sec: dict[str, list[sqlite3.Row]] = {}
    for r in conn.execute(
        "SELECT * FROM financials_annual ORDER BY security_id, fy_end ASC"
    ).fetchall():
        annual_by_sec.setdefault(r["security_id"], []).append(r)

    records_to_insert: list[tuple[Any, ...]] = []

    for sec_id in sec_ids:
        lp = latest_prices.get(sec_id)
        latest_price_date = lp[0] if lp else None
        latest_price = lp[1] if lp else None

        ann_rows = annual_by_sec.get(sec_id, [])
        latest_ann = ann_rows[-1] if ann_rows else None
        ttm = ttm_map.get(sec_id)
        notes: list[str] = []

        # Headline values from TTM / latest annual row
        latest_dps_ann, latest_dps_est = _effective_dps(latest_ann) if latest_ann else (None, False)
        dps_ttm = float(ttm["dps_ttm"]) if (ttm and ttm["dps_ttm"] is not None) else latest_dps_ann
        eps_ttm = (
            float(ttm["eps_ttm"])
            if (ttm and ttm["eps_ttm"] is not None)
            else (float(latest_ann["eps_diluted"]) if (latest_ann and latest_ann["eps_diluted"] is not None) else None)
        )

        # Dividend yield, P/E, and P/B based on implied quarter-end price
        ttm_yield = (
            (dps_ttm / latest_price)
            if (dps_ttm is not None and dps_ttm >= 0 and latest_price and latest_price > 0)
            else None
        )
        pe_ratio = (
            (latest_price / eps_ttm)
            if (eps_ttm is not None and eps_ttm > 0 and latest_price and latest_price > 0)
            else None
        )

        eq_latest = (
            float(ttm["total_equity"])
            if (ttm and ttm["total_equity"] is not None)
            else (float(latest_ann["total_equity"]) if (latest_ann and latest_ann["total_equity"] is not None) else None)
        )
        sh_latest = (
            float(ttm["shares_outstanding"])
            if (ttm and ttm["shares_outstanding"] is not None)
            else (
                float(latest_ann["shares_outstanding"] or latest_ann["shares_diluted"])
                if (latest_ann and (latest_ann["shares_outstanding"] or latest_ann["shares_diluted"]))
                else None
            )
        )
        pb_ratio = None
        if eq_latest is not None and eq_latest > 0 and sh_latest and sh_latest > 0 and latest_price and latest_price > 0:
            bps = eq_latest / sh_latest
            if bps > 0:
                pb_ratio = latest_price / bps

        # Same-fiscal-year payout ratios (DPS / EPS and aggregate DividendsPaid / FCF)
        payout_ratio_eps = None
        payout_ratio_fcf = None
        gross_margin = None
        op_margin = None
        roe = None
        roic = None
        equity_ratio = None
        fcf_margin = None
        fcf_ex_sbc_margin = None
        net_debt_to_fcf = None
        interest_coverage = None
        current_ratio = None
        share_change_5y = None

        if latest_ann is not None:
            ann_eps = latest_ann["eps_diluted"]
            if latest_dps_ann is not None and ann_eps is not None and float(ann_eps) > 0:
                payout_ratio_eps = latest_dps_ann / float(ann_eps)
            elif (
                latest_ann["dividends_paid"] is not None
                and latest_ann["net_income"] is not None
                and float(latest_ann["net_income"]) > 0
            ):
                payout_ratio_eps = float(latest_ann["dividends_paid"]) / float(latest_ann["net_income"])

            if (
                latest_ann["dividends_paid"] is not None
                and latest_ann["fcf"] is not None
                and float(latest_ann["fcf"]) > 0
            ):
                payout_ratio_fcf = float(latest_ann["dividends_paid"]) / float(latest_ann["fcf"])

            rev = latest_ann["revenue"]
            if rev is not None and float(rev) > 0:
                rev_f = float(rev)
                if latest_ann["gross_profit"] is not None:
                    gross_margin = float(latest_ann["gross_profit"]) / rev_f
                if latest_ann["operating_income"] is not None:
                    op_margin = float(latest_ann["operating_income"]) / rev_f
                if latest_ann["fcf"] is not None:
                    fcf_f = float(latest_ann["fcf"])
                    fcf_margin = fcf_f / rev_f
                    if latest_ann["sbc"] is not None:
                        fcf_ex_sbc_margin = (fcf_f - float(latest_ann["sbc"])) / rev_f

            if (
                latest_ann["net_income"] is not None
                and latest_ann["total_equity"] is not None
                and float(latest_ann["total_equity"]) > 0
            ):
                roe = float(latest_ann["net_income"]) / float(latest_ann["total_equity"])

            if (
                latest_ann["total_equity"] is not None
                and latest_ann["total_assets"] is not None
                and float(latest_ann["total_assets"]) > 0
            ):
                equity_ratio = float(latest_ann["total_equity"]) / float(latest_ann["total_assets"])

            # ROIC = NOPAT / (Total Equity + Total Debt - Cash)
            ebit = None
            if latest_ann["operating_income"] is not None:
                ebit = float(latest_ann["operating_income"])
            elif latest_ann["pretax_income"] is not None:
                ebit = float(latest_ann["pretax_income"]) + float(latest_ann["interest_expense"] or 0.0)

            if (
                ebit is not None
                and latest_ann["total_equity"] is not None
            ):
                tax_rate = 0.21
                pt = latest_ann["pretax_income"]
                tx = latest_ann["income_tax"]
                if pt is not None and float(pt) > 0 and tx is not None:
                    raw_tr = float(tx) / float(pt)
                    tax_rate = max(0.0, min(0.40, raw_tr))
                nopat = ebit * (1.0 - tax_rate)
                invested_cap = (
                    float(latest_ann["total_equity"])
                    + float(latest_ann["total_debt"] or 0.0)
                    - float(latest_ann["cash"] or 0.0)
                )
                if invested_cap > 0:
                    roic = nopat / invested_cap

            # Interest Coverage = EBIT / Interest Expense
            if ebit is not None and latest_ann["interest_expense"] is not None and float(latest_ann["interest_expense"]) > 0:
                interest_coverage = ebit / float(latest_ann["interest_expense"])

            # Net Debt / FCF
            if (
                latest_ann["total_debt"] is not None
                and latest_ann["fcf"] is not None
                and float(latest_ann["fcf"]) > 0
            ):
                net_debt = float(latest_ann["total_debt"]) - float(latest_ann["cash"] or 0.0)
                net_debt_to_fcf = net_debt / float(latest_ann["fcf"])

            # Current Ratio = Current Assets / Current Liabilities
            if (
                latest_ann["current_assets"] is not None
                and latest_ann["current_liabilities"] is not None
                and float(latest_ann["current_liabilities"]) > 0
            ):
                current_ratio = float(latest_ann["current_assets"]) / float(latest_ann["current_liabilities"])

        # 5-year share count change (negative indicates net buybacks)
        sh_series = [
            (int(str(r["fy_end"])[:4]), float(r["shares_diluted"] or r["shares_outstanding"]))
            for r in ann_rows
            if (r["shares_diluted"] or r["shares_outstanding"]) and float(r["shares_diluted"] or r["shares_outstanding"]) > 0
        ]
        if sh_series:
            ly_s, lv_s = sh_series[-1]
            v5_s, _ = _find_value_n_years_ago(sh_series, ly_s, 5)
            if v5_s is not None and v5_s > 0:
                share_change_5y = (lv_s / v5_s) - 1.0

        # Consecutive non-cut dividend streak & dividend/revenue CAGRs
        div_streak_years, div_streak_open, used_est_dps = _compute_dividend_streak(ann_rows)
        if used_est_dps or latest_dps_est:
            notes.append("DPS fallback (dividends_paid/shares) used for missing years")

        dps_series = [
            (int(str(r["fy_end"])[:4]), val)
            for r in ann_rows
            for val, _ in [_effective_dps(r)]
            if val is not None and val > 0
        ]
        rev_series = [
            (int(str(r["fy_end"])[:4]), float(r["revenue"]))
            for r in ann_rows
            if r["revenue"] is not None and float(r["revenue"]) > 0
        ]

        div_cagr_5y = None
        div_cagr_10y = None
        if dps_series:
            ly, lv = dps_series[-1]
            v5, span5 = _find_value_n_years_ago(dps_series, ly, 5)
            div_cagr_5y = _calc_cagr(v5, lv, span5)
            v10, span10 = _find_value_n_years_ago(dps_series, ly, 10)
            div_cagr_10y = _calc_cagr(v10, lv, span10)

        rev_cagr_5y = None
        if rev_series:
            ly, lv = rev_series[-1]
            v5, span5 = _find_value_n_years_ago(rev_series, ly, 5)
            rev_cagr_5y = _calc_cagr(v5, lv, span5)

        if not ann_rows:
            notes.append("No SEC XBRL companyfacts available")

        records_to_insert.append(
            (
                sec_id,
                latest_price_date,
                latest_price,
                round(ttm_yield, 6) if ttm_yield is not None else None,
                round(payout_ratio_eps, 6) if payout_ratio_eps is not None else None,
                round(payout_ratio_fcf, 6) if payout_ratio_fcf is not None else None,
                div_streak_years,
                div_streak_open,
                round(div_cagr_5y, 6) if div_cagr_5y is not None else None,
                round(div_cagr_10y, 6) if div_cagr_10y is not None else None,
                round(rev_cagr_5y, 6) if rev_cagr_5y is not None else None,
                round(gross_margin, 6) if gross_margin is not None else None,
                round(op_margin, 6) if op_margin is not None else None,
                round(roe, 6) if roe is not None else None,
                round(roic, 6) if roic is not None else None,
                round(equity_ratio, 6) if equity_ratio is not None else None,
                round(fcf_margin, 6) if fcf_margin is not None else None,
                round(fcf_ex_sbc_margin, 6) if fcf_ex_sbc_margin is not None else None,
                round(net_debt_to_fcf, 4) if net_debt_to_fcf is not None else None,
                round(interest_coverage, 4) if interest_coverage is not None else None,
                round(current_ratio, 4) if current_ratio is not None else None,
                round(share_change_5y, 6) if share_change_5y is not None else None,
                round(pe_ratio, 4) if pe_ratio is not None else None,
                round(pb_ratio, 4) if pb_ratio is not None else None,
                "; ".join(notes) if notes else None,
            )
        )

    conn.executemany(
        """
        INSERT INTO stock_metrics (
            security_id, latest_price_date, latest_price, ttm_yield,
            payout_ratio_eps, payout_ratio_fcf, div_streak_years, div_streak_open,
            div_cagr_5y, div_cagr_10y, rev_cagr_5y, gross_margin, op_margin, roe, roic,
            equity_ratio, fcf_margin, fcf_ex_sbc_margin, net_debt_to_fcf,
            interest_coverage, current_ratio, share_change_5y,
            pe_ratio, pb_ratio, data_notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        records_to_insert,
    )
    conn.commit()
    return len(records_to_insert)


def compute_etf_dividend_history(
    conn: sqlite3.Connection,
    etf_id: str,
) -> dict[str, dict[str, float | None]]:
    """Estimate annual portfolio dividends and weighted average yield across all ETF reporting dates.

    - Aligns each holding's split-adjusted SEC XBRL annual DPS back to the share basis
      on each reporting date using inferred split effective dates.
    - Excludes extreme outliers (yield > 18%) and scales the covered weighted yield to the
      ETF's full equity portfolio value.
    """
    raw_cf_dir = RAW_DIR / "edgar" / "companyfacts"

    # 1. Collect split events per security from cached companyfacts JSON
    cik_overrides = load_cik_overrides()
    cik_map = {
        r["security_id"]: r["cik"]
        for r in conn.execute(
            "SELECT security_id, cik FROM securities WHERE cik IS NOT NULL AND cik != ''"
        ).fetchall()
    }
    splits_by_chain: dict[tuple[str, ...], list[tuple[str, float]]] = {}
    sec_splits: dict[str, list[tuple[str, float]]] = {}
    for sec_id, cik in cik_map.items():
        cik_chain = get_cik_chain(sec_id, cik, cik_overrides)
        if not cik_chain:
            continue
        if cik_chain not in splits_by_chain:
            payloads: list[dict[str, Any]] = []
            for c in cik_chain:
                p = raw_cf_dir / f"CIK{c}.json"
                if p.is_file():
                    try:
                        payloads.append(json.loads(p.read_text(encoding="utf-8")))
                    except Exception:  # noqa: BLE001
                        continue
            if payloads:
                try:
                    _, sp = extract_annual_financials(merge_company_facts(payloads))
                    splits_by_chain[cik_chain] = sp
                except Exception:  # noqa: BLE001
                    splits_by_chain[cik_chain] = []
            else:
                splits_by_chain[cik_chain] = []
        if splits_by_chain[cik_chain]:
            sec_splits[sec_id] = splits_by_chain[cik_chain]

    # 2. Identify the first reporting date reflecting each split via quarter_prices jumps
    prices_by_sec: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for r in conn.execute(
        "SELECT security_id, as_of, price_usd FROM quarter_prices ORDER BY security_id, as_of"
    ).fetchall():
        prices_by_sec[r["security_id"]].append((r["as_of"], float(r["price_usd"])))

    split_effective_as_of: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for sec_id, sp_list in sec_splits.items():
        plist = prices_by_sec.get(sec_id, [])
        for filed_str, ratio in sp_list:
            d_filed = date.fromisoformat(filed_str)
            win_start = (d_filed - timedelta(days=460)).isoformat()
            best_as_of: str | None = None
            best_err = 999.0
            for i in range(1, len(plist)):
                prev_d, prev_p = plist[i - 1]
                cur_d, cur_p = plist[i]
                if cur_d < win_start or prev_d > filed_str:
                    continue
                if prev_p > 0 and cur_p > 0:
                    jump = prev_p / cur_p
                    err = abs(jump / ratio - 1.0)
                    if err < 0.35 and err < best_err:
                        best_err = err
                        best_as_of = cur_d
            eff = best_as_of or (d_filed - timedelta(days=180)).isoformat()
            split_effective_as_of[sec_id].append((eff, ratio))

    def split_factor_at(sec_id: str, as_of: str) -> float:
        factor = 1.0
        for eff, ratio in split_effective_as_of.get(sec_id, ()):
            if as_of < eff:
                factor *= ratio
        return factor

    # 3. Load effective annual DPS and TTM DPS series
    ann_dps: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for r in conn.execute(
        """
        SELECT security_id, fy_end, dps_declared, dps_paid,
               dividends_paid, shares_diluted, shares_outstanding
        FROM financials_annual
        ORDER BY security_id, fy_end ASC
        """
    ).fetchall():
        dps, _ = _effective_dps(r)
        if dps is not None and dps >= 0:
            ann_dps[r["security_id"]].append((r["fy_end"], float(dps)))

    ttm_dps = {
        r["security_id"]: float(r["dps_ttm"])
        for r in conn.execute(
            "SELECT security_id, dps_ttm FROM financials_ttm WHERE dps_ttm IS NOT NULL"
        ).fetchall()
    }

    def dps_for_date(sec_id: str, as_of: str) -> float | None:
        series = ann_dps.get(sec_id)
        if not series:
            return None
        d_target = date.fromisoformat(as_of)
        best: tuple[int, float] | None = None
        for fy_end, val in series:
            d_fy = date.fromisoformat(fy_end)
            gap = (d_target - d_fy).days
            if -185 <= gap <= 420:
                score = abs(gap) if gap >= 0 else abs(gap) + 90
                if best is None or score < best[0]:
                    best = (score, val)
        if best is not None:
            return best[1]
        if as_of >= series[-1][0]:
            if sec_id in ttm_dps:
                return ttm_dps[sec_id]
            return series[-1][1]
        return None

    # 4. Aggregate portfolio dividends per reporting date
    holdings_by_date: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in conn.execute(
        """
        SELECT as_of, security_id, shares, value_usd
        FROM etf_holdings
        WHERE etf_id = ?
        ORDER BY as_of ASC
        """,
        (etf_id,),
    ).fetchall():
        holdings_by_date[r["as_of"]].append(r)

    result: dict[str, dict[str, float | None]] = {}
    for as_of, rows in sorted(holdings_by_date.items()):
        total_val = sum(float(r["value_usd"]) for r in rows if float(r["value_usd"]) > 0)
        cov_val = 0.0
        div_sum = 0.0

        for r in rows:
            sh = float(r["shares"])
            val = float(r["value_usd"])
            if sh <= 0 or val <= 0:
                continue
            sid = r["security_id"]
            dps_latest = dps_for_date(sid, as_of)
            if dps_latest is None:
                continue
            dps_at_date = dps_latest * split_factor_at(sid, as_of)
            price = val / sh
            yld = dps_at_date / price
            # Exclude extreme outliers caused by XBRL unit quirks or dual-class ratios
            if yld < 0 or yld > 0.18:
                continue
            cov_val += val
            div_sum += sh * dps_at_date

        cov_weight = (cov_val / total_val) if total_val > 0 else 0.0
        if cov_val > 0 and cov_weight >= 0.5:
            est_yield = div_sum / cov_val
            est_total_div = total_val * est_yield
            result[as_of] = {
                "est_annual_dividends_usd": round(est_total_div, 2),
                "est_yield": round(est_yield, 6),
                "div_coverage_weight": round(cov_weight, 4),
            }
        else:
            result[as_of] = {
                "est_annual_dividends_usd": None,
                "est_yield": None,
                "div_coverage_weight": round(cov_weight, 4),
            }

    return result
