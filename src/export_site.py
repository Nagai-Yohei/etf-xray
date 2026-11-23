"""SQLite（data/market.db）から静的サイト配信用の分割 JSON（dist/data/）を生成する。

出力ファイル（DESIGN.md 第5章）:
- dist/data/meta.json
- dist/data/etf/<ETF_ID>/summary.json
- dist/data/etf/<ETF_ID>/holdings/<AS_OF>.json
- dist/data/stocks/index.json
- dist/data/stocks/<SECURITY_ID>.json
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


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    path.write_text(text, encoding="utf-8")


def _safe_filename(ident: str) -> str:
    if not _SAFE_ID_RE.match(ident):
        raise ValueError(f"Unsafe filename identifier: {ident!r}")
    return ident


def _copy_web_assets(dist_dir: Path) -> None:
    """web/ 配下の静的アセットが存在すれば dist/ へコピーする。"""
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
    """SQLite DB から dist/ 一式（静的アセット + 分割 JSON）をビルドする。"""
    data_dir = dist_dir / "data"
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
        holdings_file_count = 0

        # ------------------------------------------------------------
        # 1. ETF ごとの summary.json および holdings/<AS_OF>.json
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

            # 入替データ集計
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

            # 基準日別の保有銘柄出力 ＋ 保有期間マップ（tenure_map）集計
            tenure_acc: dict[str, dict[str, Any]] = {}
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

                _write_json(
                    data_dir / "etf" / etf_id / "holdings" / f"{as_of}.json",
                    {
                        "etf_id": etf_id,
                        "as_of": as_of,
                        "source": d_row["source"],
                        "accession": d_row["accession"],
                        "holdings_count": int(d_row["holdings_count"]),
                        "total_value_usd": round(float(d_row["total_value_usd"]), 2),
                        "holdings": out_holdings,
                    },
                )
                holdings_file_count += 1

            # 基準日ごとの推定年間配当総額・ポートフォリオ利回りを算出
            div_history = compute_etf_dividend_history(conn, etf_id)

            # summary.json 組み立て
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
            summary_changes = [
                {
                    "as_of": d["as_of"],
                    "added": changes_by_date[d["as_of"]]["added"],
                    "removed": changes_by_date[d["as_of"]]["removed"],
                }
                for d in dates
                if changes_by_date[d["as_of"]]["added"] or changes_by_date[d["as_of"]]["removed"]
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
                    "changes": summary_changes,
                    "tenure_map": tenure_list,
                },
            )

            latest_row = dates[-1]
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
                }
            )

        # ------------------------------------------------------------
        # 2. 個別株 index.json および stocks/<SECURITY_ID>.json
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

        stocks_index: dict[str, dict[str, Any]] = {}
        stock_file_count = 0

        for s in sec_rows:
            sec_id = _safe_filename(s["security_id"])
            m = metrics_map.get(sec_id) or {}
            t = ttm_map.get(sec_id) or {}
            m_clean = {k: v for k, v in m.items() if k != "security_id"}
            t_clean = {k: v for k, v in t.items() if k != "security_id"}

            active_etfs = sorted(etf_membership.get(sec_id, set()))
            all_etfs = sorted((etf_hist_by_sec.get(sec_id) or {}).keys())

            stocks_index[sec_id] = {
                "security_id": sec_id,
                "ticker": s["ticker"],
                "name": s["name"],
                "cik": s["cik"],
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
                "roic": m_clean.get("roic"),
                "equity_ratio": m_clean.get("equity_ratio"),
                "fcf_margin": m_clean.get("fcf_margin"),
                "fcf_ex_sbc_margin": m_clean.get("fcf_ex_sbc_margin"),
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

            _write_json(
                data_dir / "stocks" / f"{sec_id}.json",
                {
                    "security_id": sec_id,
                    "ticker": s["ticker"],
                    "name": s["name"],
                    "cusip": s["cusip"],
                    "isin": s["isin"],
                    "cik": s["cik"],
                    "first_seen": s["first_seen"],
                    "last_seen": s["last_seen"],
                    "current_etfs": active_etfs,
                    "metrics": m_clean,
                    "ttm": t_clean or None,
                    "annual": annual_by_sec.get(sec_id, []),
                    "quarterly": quarterly_by_sec.get(sec_id, []),
                    "prices": prices_by_sec.get(sec_id, []),
                    "etf_history": etf_hist_by_sec.get(sec_id, {}),
                },
            )
            stock_file_count += 1

        _write_json(data_dir / "stocks" / "index.json", stocks_index)

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
                "stock_count": stock_file_count,
            },
        )

        return {
            "etfs": len(meta_etfs),
            "holdings_files": holdings_file_count,
            "stock_files": stock_file_count,
        }
    finally:
        conn.close()


def main() -> int:
    stats = export_site()
    print(
        f"Exported dist/data: {stats['etfs']} ETF(s), "
        f"{stats['holdings_files']} holdings JSON, {stats['stock_files']} stock JSON"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
