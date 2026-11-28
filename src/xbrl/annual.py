"""Annual SEC XBRL financials extraction, revenue normalization, and DPS validation."""

from __future__ import annotations

import statistics
from typing import Any

from src.xbrl.records import (
    FactRecord,
    _extract_total_debt,
    _fix_share_scale,
    _is_annual_flow,
    _iter_tag_records,
    _parse_date,
    _rec_recency_key,
    _select_best_annual_flow,
    _select_best_instant_for_dates,
    _select_best_single_quarter,
)
from src.xbrl.splits import (
    _cumulative_split_factor,
    _detect_post_10k_split,
    _estimate_split_events,
)
from src.xbrl.tags import (
    ANNUAL_FORMS,
    BANK_NII_TAGS,
    BANK_NONINT_TAGS,
    COMMON_DIV_CASH_TAGS,
    PREFERRED_DIV_TAGS,
    QUARTERLY_FORMS,
    REIT_LEASE_TAGS,
    TAG_MAP_FLOW_PER_SHARE,
    TAG_MAP_FLOW_SHARES,
    TAG_MAP_FLOW_USD,
    TAG_MAP_INSTANT_USD,
)


def _normalize_annual_revenue(
    company_facts: dict[str, Any],
    flow_usd_raw: dict[str, list[FactRecord]],
    flow_usd: dict[str, dict[str, FactRecord]],
) -> None:
    """Resolve zero/partial revenue tags, gross-profit inconsistencies, bank NII synthesis, and REIT leases."""
    # Guard against partial/segment us-gaap:Revenues leaks where ASC 606 revenue is the true consolidated total
    # (e.g. GIS 2023-2024, BLK 2022-2024, OWL 2022-2025) while preserving E&P hedge-loss years
    rev_by_end_tag: dict[tuple[str, str], FactRecord] = {}
    for rec in flow_usd_raw["revenue"]:
        if not _is_annual_flow(rec):
            continue
        k = (rec.end, rec.tag)
        prev_r = rev_by_end_tag.get(k)
        if prev_r is None or _rec_recency_key(rec) > _rec_recency_key(prev_r):
            rev_by_end_tag[k] = rec

    # Replace placeholder 0.0 revenue tags with the highest-priority positive revenue tag for the same fy_end
    # (e.g. FLS 2014-2025 Revenues=0.0 vs RevenueFromContractWithCustomerExcludingAssessedTax=$4.73B)
    for fe, chosen_r in list(flow_usd["revenue"].items()):
        if chosen_r.val == 0.0:
            pos_alts = [
                rec
                for (r_fe, _r_tag), rec in rev_by_end_tag.items()
                if r_fe == fe and rec.val > 0
            ]
            if pos_alts:
                flow_usd["revenue"][fe] = min(pos_alts, key=lambda r: r.priority)

    both_rev_ends = [
        fe
        for fe in flow_usd["revenue"]
        if (fe, "us-gaap:Revenues") in rev_by_end_tag
        and (fe, "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax") in rev_by_end_tag
    ]
    all_plain_lt_asc = bool(both_rev_ends) and all(
        rev_by_end_tag[(fe, "us-gaap:Revenues")].val
        < rev_by_end_tag[(fe, "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax")].val
        for fe in both_rev_ends
    )
    latest_rev_end = max(flow_usd["revenue"]) if flow_usd["revenue"] else None
    plain_dropped_latest = (
        latest_rev_end is not None
        and (latest_rev_end, "us-gaap:Revenues") not in rev_by_end_tag
    )
    for fe in list(flow_usd["revenue"]):
        chosen_r = flow_usd["revenue"][fe]
        if chosen_r.tag != "us-gaap:Revenues":
            continue
        asc_r = rev_by_end_tag.get(
            (fe, "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax")
        )
        if asc_r is None or asc_r.val <= 0:
            continue
        if (asc_r.filed > chosen_r.filed and asc_r.val > chosen_r.val * 1.25) or (
            chosen_r.val > 0
            and asc_r.val > chosen_r.val * 1.55
            and (all_plain_lt_asc or plain_dropped_latest)
        ):
            flow_usd["revenue"][fe] = asc_r

    # Guard against partial/segment revenue leaks where GrossProfit exceeds the chosen revenue tag
    # (e.g. AIT 2011-2016 Revenues=$24M vs SalesRevenueNet=$2.75B & GrossProfit=$770M)
    for fe, gp_r in flow_usd["gross_profit"].items():
        cur_r = flow_usd["revenue"].get(fe)
        if cur_r is not None and gp_r.val > 0 and cur_r.val >= 0 and gp_r.val > cur_r.val * 1.02:
            better = [
                rec
                for (r_fe, _r_tag), rec in rev_by_end_tag.items()
                if r_fe == fe and rec.val >= gp_r.val
            ]
            if better:
                flow_usd["revenue"][fe] = min(better, key=lambda r: r.priority)

    # Bank revenue synthesis (net interest income + noninterest income):
    # apply when revenue tags are absent OR capture only non-interest fee income (e.g. FITB, RF, CPF, NBHC, ZION).
    bank_nii = _select_best_annual_flow(
        _iter_tag_records(company_facts, BANK_NII_TAGS, ("USD",), ANNUAL_FORMS)
    )
    bank_nonint = _select_best_annual_flow(
        _iter_tag_records(company_facts, BANK_NONINT_TAGS, ("USD",), ANNUAL_FORMS)
    )
    for fy_end in set(bank_nii) & set(bank_nonint):
        r1 = bank_nii[fy_end]
        r2 = bank_nonint[fy_end]
        bank_tot = r1.val + r2.val
        cur_rev = flow_usd["revenue"].get(fy_end)
        use_bank = cur_rev is None or (
            r1.val > 0
            and bank_tot > max(0.0, cur_rev.val) * 1.5
        )
        if use_bank:
            flow_usd["revenue"][fy_end] = FactRecord(
                val=bank_tot,
                end=fy_end,
                start=r1.start or r2.start,
                filed=max(r1.filed, r2.filed),
                form=r1.form,
                accn=r1.accn,
                tag="synthetic:BankNII+Noninterest",
                priority=99,
            )

    # REIT lease revenue fallback (ASC 842 rental income excluded from ASC 606 contract revenue, e.g. CPT)
    reit_lease = _select_best_annual_flow(
        _iter_tag_records(company_facts, REIT_LEASE_TAGS, ("USD",), ANNUAL_FORMS)
    )
    for fy_end, lease_r in reit_lease.items():
        if lease_r.val <= 0:
            continue
        cur_rev = flow_usd["revenue"].get(fy_end)
        if cur_rev is None:
            flow_usd["revenue"][fy_end] = lease_r
        elif (
            "RevenueFromContractWithCustomer" in cur_rev.tag
            and lease_r.val > max(0.0, cur_rev.val) * 2.0
        ):
            combined_val = lease_r.val + max(0.0, cur_rev.val)
            flow_usd["revenue"][fy_end] = FactRecord(
                val=combined_val,
                end=fy_end,
                start=lease_r.start,
                filed=max(lease_r.filed, cur_rev.filed),
                form=lease_r.form,
                accn=lease_r.accn,
                tag="synthetic:Lease+ASC606",
                priority=98,
            )


def _validate_annual_dps(
    dps_d_adj: float | None,
    dps_p_adj: float | None,
    dps_d_factor: float,
    dps_p_factor: float,
    est_dps: float | None,
) -> tuple[float | None, float | None]:
    """Validate annual per-share DPS against unit-scale corruption and single-quarter mis-tagging."""

    def _clean_per_share_dps(val: float | None, sf: float) -> float | None:
        if val is None:
            return None
        if val < 0:
            return None
        if sf >= 0.05 and val > 150.0:
            return None
        if est_dps is not None and est_dps > 0 and val > 25.0 and (val / est_dps) > 20.0:
            return None
        return val

    dps_d_adj = _clean_per_share_dps(dps_d_adj, dps_d_factor)
    dps_p_adj = _clean_per_share_dps(dps_p_adj, dps_p_factor)

    # Reject single-quarter dps_paid (~1/4 of annual implied) mis-tagged with an annual duration
    # (e.g. AA $0.10 vs $0.40, TNET $0.275 vs $1.06, AGM $1.50 vs $6.00+, AGO $0.28 vs $1.12)
    if (
        dps_p_adj is not None
        and dps_p_adj > 0.0
        and est_dps is not None
        and est_dps > 0.05
        and 0.15 <= (dps_p_adj / est_dps) <= 0.35
    ):
        dps_p_adj = None

    # Validate dps_declared against dps_paid and implied DPS (catches PLD footnote sub-item leaks,
    # ALLY 0.0 placeholders, and single-quarter values mis-tagged with annual durations)
    if dps_d_adj is not None:
        if dps_d_adj == 0.0 and (
            (dps_p_adj is not None and dps_p_adj > 0.0)
            or (est_dps is not None and est_dps > 0.05)
        ):
            dps_d_adj = None
        elif (
            dps_d_adj > 0.0
            and dps_p_adj is not None
            and dps_p_adj > 0.0
            and (dps_d_adj < dps_p_adj * 0.5 or dps_d_adj > dps_p_adj * 2.0)
        ):
            # When declared disagrees >50% with paid while implied corroborates paid, discard declared
            if (
                est_dps is None
                or (0.5 * dps_p_adj <= est_dps <= 1.5 * dps_p_adj)
                or (abs(est_dps - dps_p_adj) < abs(est_dps - dps_d_adj))
            ):
                dps_d_adj = None
        elif (
            dps_d_adj > 0.0
            and dps_p_adj is None
            and est_dps is not None
            and est_dps > 0.05
            and 0.15 <= (dps_d_adj / est_dps) <= 0.35
        ):
            # Single-quarter DPS (~1/4 of annual implied) mis-tagged as annual
            dps_d_adj = None

    return dps_d_adj, dps_p_adj


def _clear_stale_special_dps(rows: list[dict[str, Any]]) -> None:
    """Clear stale iXBRL footnote copy-paste where a prior special dividend is re-tagged verbatim (e.g. ACGL FY2025)."""
    for i in range(1, len(rows)):
        prev_r = rows[i - 1]
        cur_r = rows[i]
        cur_d = cur_r.get("dps_declared")
        prev_d = prev_r.get("dps_declared")
        if (
            cur_d is not None
            and cur_d > 0.50
            and cur_r.get("dps_paid") is None
            and prev_d is not None
            and prev_d > 0.50
            and abs(cur_d - prev_d) <= max(0.01, prev_d * 0.01)
        ):
            prev_sh = max(prev_r.get("shares_diluted") or 0.0, prev_r.get("shares_outstanding") or 0.0)
            cur_sh = max(cur_r.get("shares_diluted") or 0.0, cur_r.get("shares_outstanding") or 0.0)
            prev_dp = prev_r.get("dividends_paid")
            cur_dp = cur_r.get("dividends_paid")
            if prev_dp is not None and prev_sh > 0 and cur_dp is not None and cur_sh > 0:
                prev_est = prev_dp / prev_sh
                cur_est = cur_dp / cur_sh
                if 0.5 * prev_d <= prev_est <= 1.5 * prev_d and cur_est < cur_d * 0.05:
                    cur_r["dps_declared"] = None


def _strip_preferred_dividend(
    div_rec: FactRecord | None,
    div_paid_val: float | None,
    common_div_rec: FactRecord | None,
    pref_div_rec: FactRecord | None,
    dps_d_adj: float | None,
    dps_p_adj: float | None,
    sh_dil_adj: float | None,
    sh_out_adj: float | None,
) -> float | None:
    """Exclude preferred stock dividends when cash-flow tags report combined dividends.

    Prevents preferred payouts from inflating common-stock dividend yields, payout ratios,
    and total return (e.g. AGM, BA, COHR, OXY, BAC, GS, SCHW) while guarding against
    double-subtraction when the cash-flow tag is already common-only.
    """
    if (
        div_rec is None
        or div_paid_val is None
        or div_paid_val <= 0
        or div_rec.tag not in ("us-gaap:PaymentsOfDividends", "us-gaap:PaymentsOfOrdinaryDividends")
        or pref_div_rec is None
    ):
        return div_paid_val

    pv = abs(pref_div_rec.val)
    if pv <= 0:
        return div_paid_val

    # 1. Prefer explicit common-only equity-statement cash dividend when (common + preferred) ~= combined CF total
    if common_div_rec is not None:
        ceq = abs(common_div_rec.val)
        if ceq > 0 and ceq < div_paid_val * 0.98 and 0.65 * div_paid_val <= (ceq + pv) <= 1.35 * div_paid_val:
            return ceq

    # 2. Otherwise subtract preferred dividends only when corroborated (prevents double-subtraction):
    #    (a) DPS * shares is closer to (total - preferred) than to total (e.g. GS, VCTR, UMBF)
    #    (b) No per-share common DPS is reported and total <= 1.15 * preferred (100% preferred, e.g. BA, COHR)
    ref_dps = (
        dps_d_adj
        if (dps_d_adj is not None and dps_d_adj > 0)
        else (dps_p_adj if (dps_p_adj is not None and dps_p_adj > 0) else None)
    )
    ref_sh = max(sh_dil_adj or 0.0, sh_out_adj or 0.0)
    if ref_dps is not None and ref_sh > 0:
        est_common = ref_dps * ref_sh
        net_val = max(0.0, div_paid_val - pv)
        if abs(net_val - est_common) < abs(div_paid_val - est_common) and pv >= div_paid_val * 0.03:
            return net_val
    elif ref_dps is None and div_paid_val <= pv * 1.15:
        return None

    return div_paid_val


def extract_annual_financials(
    company_facts: dict[str, Any],
    min_year: int = 2009,
) -> tuple[list[dict[str, Any]], list[tuple[str, float]]]:
    """Extract split-adjusted annual financials and detected split events from companyfacts JSON."""
    flow_usd_raw = {
        metric: _iter_tag_records(company_facts, tags, ("USD",), ANNUAL_FORMS)
        for metric, tags in TAG_MAP_FLOW_USD.items()
    }
    flow_ps_raw = {
        metric: _iter_tag_records(company_facts, tags, ("USD/shares",), ANNUAL_FORMS)
        for metric, tags in TAG_MAP_FLOW_PER_SHARE.items()
    }
    flow_sh_raw = {
        metric: _iter_tag_records(company_facts, tags, ("shares",), ANNUAL_FORMS)
        for metric, tags in TAG_MAP_FLOW_SHARES.items()
    }

    shares_out_bs_raw = _iter_tag_records(
        company_facts,
        [("us-gaap", "CommonStockSharesOutstanding")],
        ("shares",),
        ANNUAL_FORMS,
    )

    # 1. Infer stock split events (using both diluted shares and period-end CommonStockSharesOutstanding)
    split_events = _estimate_split_events(
        shares_recs=flow_sh_raw["shares_diluted"],
        eps_recs=flow_ps_raw["eps_diluted"],
        dps_decl_recs=flow_ps_raw["dps_declared"],
        dps_paid_recs=flow_ps_raw["dps_paid"],
        shares_out_recs=shares_out_bs_raw,
    )

    # 1b. Also detect post-10-K mid-year stock splits via overlapping comparative 10-Q periods
    post_split = _detect_post_10k_split(
        company_facts,
        annual_shares_recs=flow_sh_raw["shares_diluted"],
        annual_eps_recs=flow_ps_raw["eps_diluted"],
    )
    if post_split is not None:
        split_events.append(post_split)
        split_events.sort(key=lambda item: item[0])

    # 2. Pick the best annual record per fy_end for each flow metric
    flow_usd = {m: _select_best_annual_flow(recs) for m, recs in flow_usd_raw.items()}
    flow_ps = {
        m: _select_best_annual_flow(recs, avoid_integer_rounding=True)
        for m, recs in flow_ps_raw.items()
    }
    flow_sh = {m: _select_best_annual_flow(recs) for m, recs in flow_sh_raw.items()}

    _normalize_annual_revenue(company_facts, flow_usd_raw, flow_usd)

    # Build the valid fy_end set from core PL/CF metrics (revenue / net_income / operating_cf)
    core_ends = sorted(
        {
            end
            for end in (
                set(flow_usd["revenue"])
                | set(flow_usd["net_income"])
                | set(flow_usd["operating_cf"])
            )
            if int(end[:4]) >= min_year
        }
    )

    # Deduplicate fy_end dates separated by a few days (e.g. 52/53-week calendar amendments or comparative typos)
    # while keeping normal annual period ends (>= 200 days apart)
    filtered_ends: list[str] = []
    for end in core_ends:
        d_cur = _parse_date(end)
        if d_cur is None:
            continue
        if filtered_ends:
            d_prev = _parse_date(filtered_ends[-1])
            if d_prev is not None and (d_cur - d_prev).days < 200:
                # Keep the record with more populated fields
                prev_end = filtered_ends[-1]
                prev_score = sum(
                    1
                    for m in ("revenue", "net_income", "operating_cf")
                    if prev_end in flow_usd[m]
                )
                cur_score = sum(
                    1
                    for m in ("revenue", "net_income", "operating_cf")
                    if end in flow_usd[m]
                )
                if cur_score > prev_score:
                    filtered_ends[-1] = end
                continue
        filtered_ends.append(end)

    # 3. Align balance sheet and period-end share counts (instant facts) to target fy_ends
    instant_usd = {
        metric: _select_best_instant_for_dates(
            _iter_tag_records(company_facts, TAG_MAP_INSTANT_USD[metric], ("USD",), ANNUAL_FORMS),
            filtered_ends,
        )
        for metric in (
            "total_assets",
            "current_assets",
            "total_equity",
            "current_liabilities",
            "cash",
            "goodwill",
            "intangibles",
        )
    }
    total_debt_map = _extract_total_debt(company_facts, filtered_ends)

    # Period-end shares: prefer CommonStockSharesOutstanding (+/-15d), else EntityCommonStockSharesOutstanding (up to +75d)
    shares_out_bs = _select_best_instant_for_dates(
        shares_out_bs_raw,
        filtered_ends,
        max_day_diff_before=15,
        max_day_diff_after=15,
    )
    shares_out_dei = _select_best_instant_for_dates(
        _iter_tag_records(
            company_facts,
            [("dei", "EntityCommonStockSharesOutstanding")],
            ("shares",),
            ANNUAL_FORMS,
        ),
        filtered_ends,
        max_day_diff_before=10,
        max_day_diff_after=75,
    )

    # Pre-collect single-quarter dividend payments across annual+quarterly forms as a fallback
    # for issuers that initiated dividends mid-year without tagging a full-year flow (e.g. AAPL FY2012)
    q_div_paid_all = _select_best_single_quarter(
        _iter_tag_records(
            company_facts,
            TAG_MAP_FLOW_USD["dividends_paid"],
            ("USD",),
            ANNUAL_FORMS | QUARTERLY_FORMS,
        )
    )
    common_div_eq_map = _select_best_annual_flow(
        _iter_tag_records(company_facts, COMMON_DIV_CASH_TAGS, ("USD",), ANNUAL_FORMS)
    )
    pref_div_map = _select_best_annual_flow(
        _iter_tag_records(company_facts, PREFERRED_DIV_TAGS, ("USD",), ANNUAL_FORMS)
    )

    # 4. Assemble annual rows and apply split adjustments
    rows: list[dict[str, Any]] = []
    for fy_end in filtered_ends:
        rev_rec = flow_usd["revenue"].get(fy_end)
        gp_rec = flow_usd["gross_profit"].get(fy_end)
        cor_rec = flow_usd["cost_of_revenue"].get(fy_end)
        cog_rec = flow_usd["cost_of_goods"].get(fy_end)
        cos_rec = flow_usd["cost_of_services"].get(fy_end)
        op_rec = flow_usd["operating_income"].get(fy_end)
        pretax_rec = flow_usd["pretax_income"].get(fy_end)
        ni_rec = flow_usd["net_income"].get(fy_end)
        rd_rec = flow_usd["rd_expense"].get(fy_end)
        sga_rec = flow_usd["sga_expense"].get(fy_end)
        int_rec = flow_usd["interest_expense"].get(fy_end)
        tax_rec = flow_usd["income_tax"].get(fy_end)
        ocf_rec = flow_usd["operating_cf"].get(fy_end)
        icf_rec = flow_usd["investing_cf"].get(fy_end)
        fcf_fin_rec = flow_usd["financing_cf"].get(fy_end)
        capex_rec = flow_usd["capex"].get(fy_end)
        sbc_rec = flow_usd["sbc"].get(fy_end)
        da_rec = flow_usd["da_expense"].get(fy_end)
        div_rec = flow_usd["dividends_paid"].get(fy_end)
        buyback_rec = flow_usd["buybacks"].get(fy_end)

        eps_rec = flow_ps["eps_diluted"].get(fy_end)
        dps_d_rec = flow_ps["dps_declared"].get(fy_end)
        dps_p_rec = flow_ps["dps_paid"].get(fy_end)
        sh_dil_rec = flow_sh["shares_diluted"].get(fy_end)
        sh_out_rec = shares_out_bs.get(fy_end) or shares_out_dei.get(fy_end)

        # Representative filing date and fiscal-year start date
        anchor_rec = ni_rec or rev_rec or ocf_rec or eps_rec
        fy_start = anchor_rec.start if anchor_rec else None
        filed = anchor_rec.filed if anchor_rec else None

        # Adjust each per-share metric and share count using the split factor for its own filing date
        eps_factor = _cumulative_split_factor(eps_rec.filed if eps_rec else filed, split_events)
        dps_d_factor = _cumulative_split_factor(dps_d_rec.filed if dps_d_rec else filed, split_events)
        dps_p_factor = _cumulative_split_factor(dps_p_rec.filed if dps_p_rec else filed, split_events)
        sh_dil_factor = _cumulative_split_factor(sh_dil_rec.filed if sh_dil_rec else filed, split_events)
        sh_out_factor = _cumulative_split_factor(sh_out_rec.filed if sh_out_rec else filed, split_events)

        eps_adj = (eps_rec.val / eps_factor) if (eps_rec is not None and eps_factor > 0) else None
        dps_d_adj = (dps_d_rec.val / dps_d_factor) if (dps_d_rec is not None and dps_d_factor > 0) else None
        dps_p_adj = (dps_p_rec.val / dps_p_factor) if (dps_p_rec is not None and dps_p_factor > 0) else None
        sh_dil_adj = (sh_dil_rec.val * sh_dil_factor) if (sh_dil_rec is not None and sh_dil_rec.val > 0) else None
        sh_out_adj = (sh_out_rec.val * sh_out_factor) if (sh_out_rec is not None and sh_out_rec.val > 0) else None

        # Correct 1,000x and 1,000,000x XBRL share-count unit scale errors (e.g. shares filed in thousands/millions)
        implied_sh: float | None = None
        if (
            ni_rec is not None
            and eps_adj is not None
            and abs(eps_adj) >= 0.05
            and abs(ni_rec.val) >= 1_000_000.0
        ):
            implied_sh = abs(ni_rec.val / eps_adj)
        sh_dil_adj = _fix_share_scale(sh_dil_adj, implied_sh)
        ref_sh_for_out = sh_dil_adj or implied_sh
        sh_out_adj = _fix_share_scale(sh_out_adj, ref_sh_for_out)
        # If EPS was absent (e.g. dual-class) and sh_dil looks 1e3x/1e6x smaller than sh_out, cross-check too
        if implied_sh is None and sh_dil_adj is not None and sh_out_adj is not None and sh_out_adj > 0:
            if sh_dil_adj < 100_000.0 and sh_out_adj >= 1_000_000.0:
                sh_dil_adj = _fix_share_scale(sh_dil_adj, sh_out_adj)

        # Normalize CapEx, dividends paid, buybacks, and expense line items to positive magnitudes
        rev_val = rev_rec.val if rev_rec is not None else None
        gp_val: float | None = None
        if gp_rec is not None:
            cand_gp = gp_rec.val
            # Reject GrossProfit tags exceeding Revenue (unit typo or mismatched segment tag, e.g. HCTI, SOWG)
            gp_invalid = rev_val is not None and (
                (rev_val > 0 and cand_gp > rev_val * 1.05)
                or (rev_val <= 0 and cand_gp > 0)
            )
            if not gp_invalid:
                gp_val = cand_gp
        if gp_val is None and rev_val is not None and cor_rec is not None:
            gp_val = rev_val - abs(cor_rec.val)
        elif gp_val is None and rev_val is not None and (cog_rec is not None or cos_rec is not None):
            # Sum separate goods and services cost tags before subtracting from revenue (e.g. VZ, LMT, SLB)
            total_cost = (abs(cog_rec.val) if cog_rec is not None else 0.0) + (
                abs(cos_rec.val) if cos_rec is not None else 0.0
            )
            gp_val = rev_val - total_cost

        capex_val = abs(capex_rec.val) if capex_rec is not None else None
        ocf_val = ocf_rec.val if ocf_rec is not None else None
        fcf_val = (ocf_val - capex_val) if (ocf_val is not None and capex_val is not None) else None
        div_paid_val = abs(div_rec.val) if div_rec is not None else None
        div_paid_val = _strip_preferred_dividend(
            div_rec=div_rec,
            div_paid_val=div_paid_val,
            common_div_rec=common_div_eq_map.get(fy_end),
            pref_div_rec=pref_div_map.get(fy_end),
            dps_d_adj=dps_d_adj,
            dps_p_adj=dps_p_adj,
            sh_dil_adj=sh_dil_adj,
            sh_out_adj=sh_out_adj,
        )
        if (
            div_paid_val is None
            and fy_start is not None
            and ((dps_d_adj is not None and dps_d_adj > 0) or (dps_p_adj is not None and dps_p_adj > 0))
        ):
            # Sum non-overlapping quarterly PaymentsOfDividends within [fy_start, fy_end] (e.g. AAPL FY2012)
            d_fys = _parse_date(fy_start)
            d_fye = _parse_date(fy_end)
            if d_fys is not None and d_fye is not None:
                in_fy = [
                    r
                    for r in q_div_paid_all.values()
                    if r.start
                    and _parse_date(r.start) is not None
                    and _parse_date(r.end) is not None
                    and (_parse_date(r.start) - d_fys).days >= -7
                    and (_parse_date(r.end) - d_fye).days <= 7
                    and abs(r.val) > 0
                ]
                if in_fy:
                    cand_sum = sum(abs(r.val) for r in in_fy)
                    ref_dps = dps_p_adj if (dps_p_adj is not None and dps_p_adj > 0) else dps_d_adj
                    ref_sh = max(sh_dil_adj or 0.0, sh_out_adj or 0.0)
                    # Reject partial 1-quarter sums when full-year DPS*shares is >2x larger (e.g. SPG FY2019)
                    if (
                        ref_dps is not None
                        and ref_dps > 0
                        and ref_sh > 0
                        and cand_sum < 0.50 * ref_dps * ref_sh
                    ):
                        div_paid_val = None
                    else:
                        div_paid_val = cand_sum
        buyback_val = abs(buyback_rec.val) if buyback_rec is not None else None

        # Representative split factor (based on PL filing date)
        rep_split_factor = _cumulative_split_factor(filed, split_events)

        # Reject corrupted XBRL per-share DPS facts (e.g. 1,000x/1,000,000x unit scaling errors)
        # so downstream metrics fall back cleanly to dividends_paid / shares
        denom_sh = max(sh_dil_adj or 0.0, sh_out_adj or 0.0)
        est_dps = (div_paid_val / denom_sh) if (div_paid_val is not None and denom_sh > 0) else None
        dps_d_adj, dps_p_adj = _validate_annual_dps(
            dps_d_adj, dps_p_adj, dps_d_factor, dps_p_factor, est_dps
        )

        rows.append(
            {
                "fy_end": fy_end,
                "fy_start": fy_start,
                "filed": filed,
                "revenue": rev_val,
                "gross_profit": gp_val,
                "operating_income": op_rec.val if op_rec else None,
                "pretax_income": pretax_rec.val if pretax_rec else None,
                "net_income": ni_rec.val if ni_rec else None,
                "rd_expense": abs(rd_rec.val) if rd_rec else None,
                "sga_expense": abs(sga_rec.val) if sga_rec else None,
                "interest_expense": abs(int_rec.val) if int_rec else None,
                "income_tax": tax_rec.val if tax_rec else None,
                "eps_diluted": round(eps_adj, 4) if eps_adj is not None else None,
                "shares_diluted": sh_dil_adj,
                "shares_outstanding": sh_out_adj,
                "operating_cf": ocf_val,
                "investing_cf": icf_rec.val if icf_rec is not None else None,
                "financing_cf": fcf_fin_rec.val if fcf_fin_rec is not None else None,
                "capex": capex_val,
                "fcf": fcf_val,
                "sbc": abs(sbc_rec.val) if sbc_rec else None,
                "da_expense": abs(da_rec.val) if da_rec else None,
                "dividends_paid": div_paid_val,
                "buybacks": buyback_val,
                "total_assets": instant_usd["total_assets"][fy_end].val
                if (fy_end in instant_usd["total_assets"] and instant_usd["total_assets"][fy_end].val > 0)
                else None,
                "current_assets": instant_usd["current_assets"][fy_end].val
                if fy_end in instant_usd["current_assets"]
                else None,
                "total_equity": instant_usd["total_equity"][fy_end].val
                if fy_end in instant_usd["total_equity"]
                else None,
                "current_liabilities": instant_usd["current_liabilities"][fy_end].val
                if fy_end in instant_usd["current_liabilities"]
                else None,
                "cash": instant_usd["cash"][fy_end].val
                if (
                    fy_end in instant_usd["cash"]
                    and instant_usd["cash"][fy_end].val >= 0
                    and not (
                        fy_end in instant_usd["total_assets"]
                        and instant_usd["total_assets"][fy_end].val > 0
                        and instant_usd["cash"][fy_end].val > instant_usd["total_assets"][fy_end].val * 1.05
                    )
                )
                else None,
                "total_debt": total_debt_map.get(fy_end)
                if (total_debt_map.get(fy_end) is not None and total_debt_map[fy_end] >= 0)
                else None,
                "goodwill": instant_usd["goodwill"][fy_end].val
                if fy_end in instant_usd["goodwill"]
                else None,
                "intangibles": instant_usd["intangibles"][fy_end].val
                if (fy_end in instant_usd["intangibles"] and instant_usd["intangibles"][fy_end].val >= 0)
                else None,
                "dps_declared": round(dps_d_adj, 4) if dps_d_adj is not None else None,
                "dps_paid": round(dps_p_adj, 4) if dps_p_adj is not None else None,
                "split_factor": rep_split_factor,
            }
        )

    # Cross-year median share-scale normalization (catches isolated 1,000x / 1,000,000x unit errors
    # on rows where EPS was absent, e.g. BRK.B 2009)
    for field in ("shares_diluted", "shares_outstanding"):
        vals = [r[field] for r in rows if r[field] is not None and r[field] > 0]
        if len(vals) >= 3:
            med = statistics.median(vals)
            for r in rows:
                if r[field] is not None and r[field] > 0:
                    r[field] = _fix_share_scale(r[field], med)

    _clear_stale_special_dps(rows)

    return rows, split_events
