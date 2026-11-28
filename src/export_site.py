"""Generate split static JSON files (dist/data/) from SQLite (data/market.db).

Output files:
- dist/data/meta.json
- dist/data/compare/overview.json
- dist/data/etf/<ETF_ID>/summary.json
- dist/data/etf/<ETF_ID>/tenure.json
- dist/data/etf/<ETF_ID>/holdings/<AS_OF>.json
- dist/data/stocks/search-active.json
- dist/data/stocks/index-active.json
- dist/data/stocks/index-former.json
- dist/data/stocks/shards/<00..ff>.json
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.db import connect
from src.env import DB_PATH, DIST_DIR, WEB_DIR
from src.metrics import compute_etf_dividend_history

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _strip_nulls(obj: Any) -> Any:
    """Recursively omit dictionary entries whose value is None."""
    if isinstance(obj, dict):
        return {k: _strip_nulls(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [_strip_nulls(item) for item in obj]
    return obj


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    compact = _strip_nulls(payload)
    text = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
    path.write_text(text, encoding="utf-8")


def _build_compare_overview(
    etf_ids: list[str],
    latest_holdings_by_etf: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Precompute the N x N ETF overlap matrix and multi-ETF core holdings (held in >= 2 ETFs)."""
    weight_maps: dict[str, dict[str, float]] = {}
    by_sec: dict[str, dict[str, Any]] = {}

    for eid in etf_ids:
        wmap: dict[str, float] = {}
        for h in latest_holdings_by_etf.get(eid) or []:
            sid = str(h["security_id"])
            w = float(h.get("weight") or 0.0)
            wmap[sid] = w
            entry = by_sec.get(sid)
            if entry is None:
                by_sec[sid] = {
                    "security_id": sid,
                    "ticker": h.get("ticker") or sid,
                    "name": h.get("name") or sid,
                    "etfs": [eid],
                    "weights": {eid: round(w, 6)},
                    "total_weight": w,
                }
            else:
                entry["etfs"].append(eid)
                entry["weights"][eid] = round(w, 6)
                entry["total_weight"] += w
        weight_maps[eid] = wmap

    matrix: dict[str, dict[str, dict[str, Any]]] = {}
    for a in etf_ids:
        row: dict[str, dict[str, Any]] = {}
        map_a = weight_maps[a]
        for b in etf_ids:
            if a == b:
                row[b] = {"overlapWeight": 1.0, "commonCount": len(map_a), "self": True}
                continue
            map_b = weight_maps[b]
            ov = 0.0
            cnt = 0
            # Iterate over the smaller holdings map for speed
            small, large = (map_a, map_b) if len(map_a) <= len(map_b) else (map_b, map_a)
            for sid, w1 in small.items():
                w2 = large.get(sid)
                if w2 is not None:
                    cnt += 1
                    ov += w1 if w1 < w2 else w2
            row[b] = {"overlapWeight": round(ov, 6), "commonCount": cnt, "self": False}
        matrix[a] = row

    core_holdings: list[dict[str, Any]] = []
    for entry in by_sec.values():
        etf_cnt = len(entry["etfs"])
        if etf_cnt >= 2:
            core_holdings.append(
                {
                    "security_id": entry["security_id"],
                    "ticker": entry["ticker"],
                    "name": entry["name"],
                    "etfs": entry["etfs"],
                    "weights": entry["weights"],
                    "etf_count": etf_cnt,
                    "avg_weight": round(entry["total_weight"] / etf_cnt, 6),
                }
            )
    core_holdings.sort(
        key=lambda x: (-x["etf_count"], -x["avg_weight"], x["security_id"])
    )

    return {
        "etf_ids": etf_ids,
        "matrix": matrix,
        "core_holdings": core_holdings,
    }


def _safe_filename(ident: str) -> str:
    if not _SAFE_ID_RE.match(ident):
        raise ValueError(f"Unsafe filename identifier: {ident!r}")
    return ident


def stock_shard_id(security_id: str) -> str:
    """Return a deterministic 2-hex-digit shard ID (FNV-1a 32-bit mod 256).

    Must match `stockShardId` in `web/js/api.js` identically.
    """
    h = 0x811C9DC5
    for b in security_id.encode("utf-8"):
        h ^= b
        h = (h * 0x01000193) & 0xFFFFFFFF
    return f"{h & 0xFF:02x}"


def _copy_web_assets(dist_dir: Path) -> None:
    """Copy static frontend assets from web/ into dist/."""
    if not WEB_DIR.is_dir():
        return
    for item in WEB_DIR.iterdir():
        dest = dist_dir / item.name
        if item.is_dir():
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(item, dest)
        else:
            shutil.copy2(item, dest)


def export_site(db_path: Path = DB_PATH, dist_dir: Path = DIST_DIR) -> dict[str, int]:
    """Build the complete dist/ bundle (static assets + split JSON) from SQLite."""
    dist_dir.mkdir(parents=True, exist_ok=True)
    final_data_dir = dist_dir / "data"
    # Stage JSON output into a temp directory and swap atomically so live preview never sees 404s
    data_dir = dist_dir / ".data_staging"
    if data_dir.exists():
        shutil.rmtree(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    _copy_web_assets(dist_dir)

    conn = connect(db_path)
    try:
        etfs = conn.execute(
            "SELECT * FROM etfs WHERE active = 1 ORDER BY etf_id"
        ).fetchall()

        meta_etfs: list[dict[str, Any]] = []
        etf_membership: dict[str, set[str]] = {}
        latest_holdings_by_etf: dict[str, list[dict[str, Any]]] = {}
        holdings_file_count = 0
        splits_cache: dict[tuple[str, ...], list[tuple[str, float]]] = {}

        # ------------------------------------------------------------
        # 1. Per-ETF summary.json, tenure.json, and holdings/<AS_OF>.json
        # ------------------------------------------------------------
        for etf in etfs:
            etf_id = _safe_filename(etf["etf_id"])
            dates = conn.execute(
                """
                SELECT as_of, holdings_count, total_value_usd, source, accession
                FROM etf_dates
                WHERE etf_id = ?
                ORDER BY as_of ASC
                """,
                (etf_id,),
            ).fetchall()

            if not dates:
                continue

            date_list = [d["as_of"] for d in dates]
            date_index = {d: i for i, d in enumerate(date_list)}
            latest_as_of = date_list[-1]

            # Aggregate constituent additions and removals
            changes_by_date: dict[str, dict[str, list[dict[str, Any]]]] = {
                d: {"added": [], "removed": []} for d in date_list
            }
            for ch in conn.execute(
                """
                SELECT as_of, kind, security_id, ticker, name, weight
                FROM etf_changes
                WHERE etf_id = ?
                ORDER BY as_of ASC, weight DESC
                """,
                (etf_id,),
            ).fetchall():
                bucket = "added" if ch["kind"] == "add" else "removed"
                changes_by_date[ch["as_of"]][bucket].append(
                    {
                        "security_id": ch["security_id"],
                        "ticker": ch["ticker"],
                        "name": ch["name"],
                        "weight": round(float(ch["weight"]), 6) if ch["weight"] is not None else None,
                    }
                )

            # Export per-date holdings and accumulate tenure_map
            tenure_acc: dict[str, dict[str, Any]] = {}
            latest_top_holdings: list[dict[str, Any]] = []
            for d_row in dates:
                as_of = d_row["as_of"]
                d_idx = date_index[as_of]
                h_rows = conn.execute(
                    """
                    SELECT h.rank, h.security_id, h.ticker, h.reported_name,
                           h.shares, h.value_usd, h.weight, h.pct_net_assets,
                           h.prev_weight, h.is_new
                    FROM etf_holdings h
                    WHERE h.etf_id = ? AND h.as_of = ?
                    ORDER BY h.rank ASC
                    """,
                    (etf_id, as_of),
                ).fetchall()

                out_holdings: list[dict[str, Any]] = []
                for h in h_rows:
                    sec_id = h["security_id"]
                    weight = float(h["weight"])
                    prev_w = float(h["prev_weight"]) if h["prev_weight"] is not None else None
                    qoq_diff = round(weight - prev_w, 6) if prev_w is not None else None
                    price_usd = (
                        round(float(h["value_usd"]) / float(h["shares"]), 4)
                        if float(h["shares"]) > 0
                        else None
                    )
                    out_holdings.append(
                        {
                            "rank": int(h["rank"]),
                            "security_id": sec_id,
                            "ticker": h["ticker"],
                            "name": h["reported_name"],
                            "shares": round(float(h["shares"]), 4),
                            "value_usd": round(float(h["value_usd"]), 2),
                            "weight": round(weight, 6),
                            "pct_net_assets": round(float(h["pct_net_assets"]), 6)
                            if h["pct_net_assets"] is not None
                            else None,
                            "prev_weight": round(prev_w, 6) if prev_w is not None else None,
                            "qoq_weight_diff": qoq_diff,
                            "is_new": bool(h["is_new"]),
                            "price_usd": price_usd,
                        }
                    )

                    if sec_id not in tenure_acc:
                        tenure_acc[sec_id] = {
                            "security_id": sec_id,
                            "ticker": h["ticker"],
                            "name": h["reported_name"],
                            "first_seen": as_of,
                            "last_seen": as_of,
                            "indices": [d_idx],
                            "latest_weight": weight if as_of == latest_as_of else None,
                        }
                    else:
                        t_item = tenure_acc[sec_id]
                        t_item["name"] = h["reported_name"]
                        t_item["last_seen"] = as_of
                        t_item["indices"].append(d_idx)
                        if as_of == latest_as_of:
                            t_item["latest_weight"] = weight

                    if as_of == latest_as_of:
                        etf_membership.setdefault(sec_id, set()).add(etf_id)

                if as_of == latest_as_of:
                    latest_holdings_by_etf[etf_id] = out_holdings
                    latest_top_holdings = [
                        {
                            "security_id": item["security_id"],
                            "ticker": item["ticker"],
                            "name": item["name"],
                            "weight": item["weight"],
                        }
                        for item in out_holdings[:5]
                    ]

                date_changes = changes_by_date[as_of]
                _write_json(
                    data_dir / "etf" / etf_id / "holdings" / f"{as_of}.json",
                    {
                        "etf_id": etf_id,
                        "as_of": as_of,
                        "source": d_row["source"],
                        "accession": d_row["accession"],
                        "holdings_count": int(d_row["holdings_count"]),
                        "total_value_usd": round(float(d_row["total_value_usd"]), 2),
                        "added": date_changes["added"],
                        "removed": date_changes["removed"],
                        "holdings": out_holdings,
                    },
                )
                holdings_file_count += 1

            # Compute estimated annual dividends and portfolio yield per reporting date
            div_history = compute_etf_dividend_history(conn, etf_id, splits_cache=splits_cache)

            # Build lightweight summary.json (changes live in holdings/<as_of>.json; tenure lives in tenure.json)
            summary_dates = [
                {
                    "as_of": d["as_of"],
                    "holdings_count": int(d["holdings_count"]),
                    "total_value_usd": round(float(d["total_value_usd"]), 2),
                    "source": d["source"],
                    "added_count": len(changes_by_date[d["as_of"]]["added"]),
                    "removed_count": len(changes_by_date[d["as_of"]]["removed"]),
                    "est_annual_dividends_usd": (div_history.get(d["as_of"]) or {}).get(
                        "est_annual_dividends_usd"
                    ),
                    "est_yield": (div_history.get(d["as_of"]) or {}).get("est_yield"),
                    "div_coverage_weight": (div_history.get(d["as_of"]) or {}).get(
                        "div_coverage_weight"
                    ),
                }
                for d in dates
            ]

            tenure_list = sorted(
                (
                    {
                        "security_id": item["security_id"],
                        "ticker": item["ticker"],
                        "name": item["name"],
                        "first_seen": item["first_seen"],
                        "last_seen": item["last_seen"],
                        "quarters_held": len(item["indices"]),
                        "in_latest": item["latest_weight"] is not None,
                        "latest_weight": round(item["latest_weight"], 6)
                        if item["latest_weight"] is not None
                        else None,
                        "date_indices": item["indices"],
                    }
                    for item in tenure_acc.values()
                ),
                key=lambda x: (not x["in_latest"], -x["quarters_held"], -(x["latest_weight"] or 0.0)),
            )
            tenure_current_count = sum(1 for item in tenure_list if item["in_latest"])

            _write_json(
                data_dir / "etf" / etf_id / "summary.json",
                {
                    "etf_id": etf_id,
                    "name": etf["name"],
                    "issuer": etf["issuer"],
                    "cik": etf["cik"],
                    "series_id": etf["series_id"],
                    "structure": etf["structure"],
                    "latest_as_of": latest_as_of,
                    "dates": summary_dates,
                    "tenure_current_count": tenure_current_count,
                    "tenure_total_count": len(tenure_list),
                },
            )
            _write_json(
                data_dir / "etf" / etf_id / "tenure.json",
                {
                    "etf_id": etf_id,
                    "latest_as_of": latest_as_of,
                    "dates": date_list,
                    "current_count": tenure_current_count,
                    "total_count": len(tenure_list),
                    "tenure_map": tenure_list,
                },
            )

            latest_row = dates[-1]
            latest_div = div_history.get(latest_as_of) or {}
            meta_etfs.append(
                {
                    "etf_id": etf_id,
                    "name": etf["name"],
                    "issuer": etf["issuer"],
                    "latest_as_of": latest_as_of,
                    "first_as_of": date_list[0],
                    "date_count": len(date_list),
                    "latest_holdings_count": int(latest_row["holdings_count"]),
                    "latest_total_value_usd": round(float(latest_row["total_value_usd"]), 2),
                    "latest_est_yield": latest_div.get("est_yield"),
                    "latest_est_annual_dividends_usd": latest_div.get("est_annual_dividends_usd"),
                    "top_holdings": latest_top_holdings,
                }
            )

        # ------------------------------------------------------------
        # 1b. Precomputed compare/overview.json (overlap matrix + core holdings)
        # ------------------------------------------------------------
        exported_etf_ids = [e["etf_id"] for e in meta_etfs]
        _write_json(
            data_dir / "compare" / "overview.json",
            _build_compare_overview(exported_etf_ids, latest_holdings_by_etf),
        )

        # ------------------------------------------------------------
        # 2. Individual stock index.json and stocks/<SECURITY_ID>.json
        # ------------------------------------------------------------
        sec_rows = conn.execute("SELECT * FROM securities ORDER BY security_id").fetchall()
        metrics_map = {
            r["security_id"]: dict(r)
            for r in conn.execute("SELECT * FROM stock_metrics").fetchall()
        }
        ttm_map = {
            r["security_id"]: dict(r)
            for r in conn.execute("SELECT * FROM financials_ttm").fetchall()
        }

        annual_by_sec: dict[str, list[dict[str, Any]]] = {}
        for r in conn.execute(
            "SELECT * FROM financials_annual ORDER BY security_id, fy_end ASC"
        ).fetchall():
            d = dict(r)
            sec_id = d.pop("security_id")
            annual_by_sec.setdefault(sec_id, []).append(d)

        quarterly_by_sec: dict[str, list[dict[str, Any]]] = {}
        for r in conn.execute(
            "SELECT * FROM financials_quarterly ORDER BY security_id, period_end ASC"
        ).fetchall():
            d = dict(r)
            sec_id = d.pop("security_id")
            quarterly_by_sec.setdefault(sec_id, []).append(d)

        prices_by_sec: dict[str, list[dict[str, Any]]] = {}
        for r in conn.execute(
            "SELECT security_id, as_of, price_usd, source_etf FROM quarter_prices ORDER BY security_id, as_of ASC"
        ).fetchall():
            prices_by_sec.setdefault(r["security_id"], []).append(
                {
                    "as_of": r["as_of"],
                    "price_usd": float(r["price_usd"]),
                    "source_etf": r["source_etf"],
                }
            )

        etf_hist_by_sec: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for r in conn.execute(
            """
            SELECT security_id, etf_id, as_of, rank, weight, shares, value_usd
            FROM etf_holdings
            ORDER BY security_id, etf_id, as_of ASC
            """
        ).fetchall():
            etf_hist_by_sec.setdefault(r["security_id"], {}).setdefault(r["etf_id"], []).append(
                {
                    "as_of": r["as_of"],
                    "rank": int(r["rank"]),
                    "weight": round(float(r["weight"]), 6),
                    "shares": round(float(r["shares"]), 4),
                    "value_usd": round(float(r["value_usd"]), 2),
                }
            )

        stocks_index_active: dict[str, dict[str, Any]] = {}
        stocks_index_former: dict[str, dict[str, Any]] = {}
        sharded_stocks: dict[str, dict[str, dict[str, Any]]] = {}
        stock_count = 0

        for s in sec_rows:
            sec_id = _safe_filename(s["security_id"])
            m = metrics_map.get(sec_id) or {}
            t = ttm_map.get(sec_id) or {}
            m_clean = {k: v for k, v in m.items() if k != "security_id"}
            t_clean = {k: v for k, v in t.items() if k != "security_id"}

            active_etfs = sorted(etf_membership.get(sec_id, set()))
            all_etfs = sorted((etf_hist_by_sec.get(sec_id) or {}).keys())

            index_entry = {
                "security_id": sec_id,
                "ticker": s["ticker"],
                "name": s["name"],
                "cik": s["cik"],
                "sector_group": s["sector_group"],
                "current_etfs": active_etfs,
                "all_etfs": all_etfs,
                "latest_price_date": m_clean.get("latest_price_date"),
                "latest_price": m_clean.get("latest_price"),
                "ttm_yield": m_clean.get("ttm_yield"),
                "payout_ratio_eps": m_clean.get("payout_ratio_eps"),
                "payout_ratio_fcf": m_clean.get("payout_ratio_fcf"),
                "div_streak_years": m_clean.get("div_streak_years"),
                "div_streak_open": bool(m_clean.get("div_streak_open"))
                if m_clean.get("div_streak_open") is not None
                else None,
                "div_cagr_5y": m_clean.get("div_cagr_5y"),
                "div_cagr_10y": m_clean.get("div_cagr_10y"),
                "rev_cagr_5y": m_clean.get("rev_cagr_5y"),
                "gross_margin": m_clean.get("gross_margin"),
                "op_margin": m_clean.get("op_margin"),
                "roe": m_clean.get("roe"),
                "roe_5y_median": m_clean.get("roe_5y_median"),
                "roic": m_clean.get("roic"),
                "equity_ratio": m_clean.get("equity_ratio"),
                "fcf_margin": m_clean.get("fcf_margin"),
                "fcf_ex_sbc_margin": m_clean.get("fcf_ex_sbc_margin"),
                "fcf_cagr_5y": m_clean.get("fcf_cagr_5y"),
                "fcf_positive_years": m_clean.get("fcf_positive_years"),
                "net_debt_to_fcf": m_clean.get("net_debt_to_fcf"),
                "interest_coverage": m_clean.get("interest_coverage"),
                "current_ratio": m_clean.get("current_ratio"),
                "share_change_5y": m_clean.get("share_change_5y"),
                "pe_ratio": m_clean.get("pe_ratio"),
                "pb_ratio": m_clean.get("pb_ratio"),
                "revenue_ttm": t_clean.get("revenue_ttm"),
                "net_income_ttm": t_clean.get("net_income_ttm"),
                "fcf_ttm": t_clean.get("fcf_ttm"),
                "eps_ttm": t_clean.get("eps_ttm"),
                "dps_ttm": t_clean.get("dps_ttm"),
            }
            if active_etfs:
                stocks_index_active[sec_id] = index_entry
            else:
                stocks_index_former[sec_id] = index_entry

            hist_by_etf = etf_hist_by_sec.get(sec_id, {})
            all_hist_dates = sorted(
                {row["as_of"] for rows in hist_by_etf.values() for row in rows}
            )
            eff_first_seen = all_hist_dates[0] if all_hist_dates else s["first_seen"]
            eff_last_seen = all_hist_dates[-1] if all_hist_dates else s["last_seen"]

            shard = stock_shard_id(sec_id)
            sharded_stocks.setdefault(shard, {})[sec_id] = {
                "security_id": sec_id,
                "ticker": s["ticker"],
                "name": s["name"],
                "cusip": s["cusip"],
                "isin": s["isin"],
                "cik": s["cik"],
                "sic": s["sic"],
                "sic_description": s["sic_description"],
                "sector_group": s["sector_group"],
                "first_seen": eff_first_seen,
                "last_seen": eff_last_seen,
                "current_etfs": active_etfs,
                "metrics": m_clean,
                "ttm": t_clean or None,
                "annual": annual_by_sec.get(sec_id, []),
                "quarterly": quarterly_by_sec.get(sec_id, []),
                "prices": prices_by_sec.get(sec_id, []),
                "etf_history": hist_by_etf,
            }
            stock_count += 1

        for shard in sorted(sharded_stocks):
            _write_json(data_dir / "stocks" / "shards" / f"{shard}.json", sharded_stocks[shard])

        _write_json(data_dir / "stocks" / "index-active.json", stocks_index_active)
        _write_json(data_dir / "stocks" / "index-former.json", stocks_index_former)

        # Lightweight search index for initial page load (avoids fetching full 2MB metrics index on Home/Stock views)
        search_active = [
            {
                "security_id": item["security_id"],
                "ticker": item["ticker"],
                "name": item["name"],
                "current_etfs": item["current_etfs"],
                "ttm_yield": item["ttm_yield"],
                "latest_price_date": item["latest_price_date"],
            }
            for item in stocks_index_active.values()
        ]
        _write_json(data_dir / "stocks" / "search-active.json", search_active)

        # Precompute Home View highlight tables so Home renders directly from meta.json
        active_vals = list(stocks_index_active.values())
        multi_etf_highlights = sorted(
            (s for s in active_vals if len(s.get("current_etfs") or []) >= 3),
            key=lambda s: (-len(s.get("current_etfs") or []), -(s.get("revenue_ttm") or 0.0), s["security_id"]),
        )[:8]
        div_leader_highlights = sorted(
            (
                s
                for s in active_vals
                if (s.get("div_streak_years") or 0) >= 10 and (s.get("ttm_yield") or 0.0) > 0
            ),
            key=lambda s: (
                -(s.get("div_streak_years") or 0),
                -len(s.get("current_etfs") or []),
                -(s.get("div_cagr_5y") if s.get("div_cagr_5y") is not None else -999.0),
                s["security_id"],
            ),
        )[:8]

        # ------------------------------------------------------------
        # 3. meta.json
        # ------------------------------------------------------------
        _write_json(
            data_dir / "meta.json",
            {
                "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "data_sources": [
                    "SEC EDGAR (Form N-PORT-P, N-CSR, N-CSRS, N-Q, 10-K, 10-Q XBRL CompanyFacts)",
                    "OpenFIGI (Public Security Identifier Mapping)",
                ],
                "disclaimer": (
                    "All data is compiled exclusively from public SEC EDGAR filings and OpenFIGI. "
                    "Prices reflect quarter-end implied values derived from ETF holdings (value / shares). "
                    "For informational purposes only; not investment advice."
                ),
                "etfs": meta_etfs,
                "highlights": {
                    "multi_etf": [
                        {
                            "security_id": s["security_id"],
                            "ticker": s["ticker"],
                            "name": s["name"],
                            "current_etfs": s["current_etfs"],
                            "ttm_yield": s["ttm_yield"],
                            "pe_ratio": s["pe_ratio"],
                        }
                        for s in multi_etf_highlights
                    ],
                    "div_leaders": [
                        {
                            "security_id": s["security_id"],
                            "ticker": s["ticker"],
                            "name": s["name"],
                            "div_streak_years": s["div_streak_years"],
                            "div_streak_open": s["div_streak_open"],
                            "ttm_yield": s["ttm_yield"],
                            "div_cagr_5y": s["div_cagr_5y"],
                        }
                        for s in div_leader_highlights
                    ],
                },
                "stock_count": stock_count,
                "active_stock_count": len(stocks_index_active),
                "former_stock_count": len(stocks_index_former),
                "stock_shard_count": len(sharded_stocks),
            },
        )

        # Atomically swap staged JSON directory into dist/data
        old_data_dir = dist_dir / ".data_old"
        if old_data_dir.exists():
            shutil.rmtree(old_data_dir)
        if final_data_dir.exists():
            final_data_dir.rename(old_data_dir)
        data_dir.rename(final_data_dir)
        if old_data_dir.exists():
            shutil.rmtree(old_data_dir)

        return {
            "etfs": len(meta_etfs),
            "holdings_files": holdings_file_count,
            "stock_files": len(sharded_stocks),
            "stock_count": stock_count,
        }
    finally:
        conn.close()
        if data_dir.exists():
            shutil.rmtree(data_dir)


def main() -> int:
    stats = export_site()
    print(
        f"Exported dist/data: {stats['etfs']} ETF(s), "
        f"{stats['holdings_files']} holdings JSON, {stats['stock_files']} stock JSON"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
