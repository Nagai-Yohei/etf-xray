"""etf-xray 全データパイプラインのオーケストレータ。

実行例:
    python -m src.pipeline                # 全ステップ実行（ETF取得 → XBRL → 指標 → dist出力）
    python -m src.pipeline --skip-xbrl    # ETF構成のみ更新
    python -m src.pipeline --only-export  # DB から dist/data のみ再出力
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from src.adapters.nport import FilingHoldings, fetch_etf_nport_filings
from src.adapters.schwab_legacy import fetch_schwab_legacy_filings
from src.adapters.vanguard_legacy import fetch_vanguard_legacy_filings
from src.db import init_db
from src.edgar import EdgarClient
from src.env import CONFIG_DIR, DB_PATH
from src.export_site import export_site
from src.fundamentals import (
    extract_annual_financials,
    extract_quarterly_financials,
    extract_ttm_financials,
)
from src.identify import ResolvedHolding, resolve_all_filings
from src.metrics import rebuild_quarter_prices, rebuild_stock_metrics

ETFS_CONFIG_PATH = CONFIG_DIR / "etfs.json"


def load_etf_configs(path: Path = ETFS_CONFIG_PATH) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [cfg for cfg in data if cfg.get("active", True)]


def fetch_etf_filings(client: EdgarClient, etf_cfg: dict[str, Any]) -> list[FilingHoldings]:
    """ETF 設定に従い、旧書類アダプタ + 共通 N-PORT-P アダプタで全基準日を取得する。"""
    filings: list[FilingHoldings] = []
    legacy_name = etf_cfg.get("legacy_adapter")
    if legacy_name == "schwab_legacy":
        filings.extend(fetch_schwab_legacy_filings(client, etf_cfg))
    elif legacy_name == "vanguard_legacy":
        filings.extend(fetch_vanguard_legacy_filings(client, etf_cfg))

    nport_filings = fetch_etf_nport_filings(client, etf_cfg)

    # 同一基準日が重複した場合は N-PORT-P を優先
    by_as_of: dict[str, FilingHoldings] = {}
    for f in filings:
        by_as_of[f.as_of] = f
    for f in nport_filings:
        by_as_of[f.as_of] = f

    return [by_as_of[d] for d in sorted(by_as_of)]


def save_etf_and_holdings(
    conn: sqlite3.Connection,
    etf_cfg: dict[str, Any],
    filings: list[FilingHoldings],
    resolved_by_as_of: dict[str, list[ResolvedHolding]],
    securities_catalog: dict[str, dict[str, Any]],
) -> None:
    """ETF 構成データと銘柄マスタを SQLite に保存し、入替履歴（etf_changes）を構築する。"""
    etf_id = etf_cfg["etf_id"]

    # 1. etfs テーブル UPSERT
    conn.execute(
        """
        INSERT INTO etfs (etf_id, name, issuer, cik, series_id, structure, fiscal_year_end_month, active)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(etf_id) DO UPDATE SET
            name = excluded.name,
            issuer = excluded.issuer,
            cik = excluded.cik,
            series_id = excluded.series_id,
            structure = excluded.structure,
            fiscal_year_end_month = excluded.fiscal_year_end_month,
            active = excluded.active
        """,
        (
            etf_id,
            etf_cfg["name"],
            etf_cfg["issuer"],
            etf_cfg["cik"],
            etf_cfg.get("series_id"),
            etf_cfg.get("structure", "open_end"),
            etf_cfg.get("fiscal_year_end_month"),
            1 if etf_cfg.get("active", True) else 0,
        ),
    )

    # 2. securities テーブル UPSERT（複数 ETF 間で first_seen/last_seen を統合）
    for sec in securities_catalog.values():
        conn.execute(
            """
            INSERT INTO securities (
                security_id, ticker, name, cusip, isin, cik, ticker_source, first_seen, last_seen
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(security_id) DO UPDATE SET
                ticker = COALESCE(excluded.ticker, securities.ticker),
                name = CASE WHEN excluded.last_seen >= COALESCE(securities.last_seen, '')
                            THEN excluded.name ELSE securities.name END,
                cusip = COALESCE(excluded.cusip, securities.cusip),
                isin = COALESCE(excluded.isin, securities.isin),
                cik = COALESCE(excluded.cik, securities.cik),
                ticker_source = CASE WHEN excluded.ticker_source IN ('manual_cusip', 'openfigi')
                                     THEN excluded.ticker_source ELSE securities.ticker_source END,
                first_seen = MIN(COALESCE(securities.first_seen, excluded.first_seen), excluded.first_seen),
                last_seen = MAX(COALESCE(securities.last_seen, excluded.last_seen), excluded.last_seen)
            """,
            (
                sec["security_id"],
                sec["ticker"],
                sec["name"],
                sec["cusip"],
                sec["isin"],
                sec["cik"],
                sec["ticker_source"],
                sec["first_seen"],
                sec["last_seen"],
            ),
        )

    # 3. 対象 ETF の既存保有・入替・基準日を洗い替え
    conn.execute("DELETE FROM etf_changes WHERE etf_id = ?", (etf_id,))
    conn.execute("DELETE FROM etf_holdings WHERE etf_id = ?", (etf_id,))
    conn.execute("DELETE FROM etf_dates WHERE etf_id = ?", (etf_id,))

    prev_holdings_map: dict[str, ResolvedHolding] | None = None

    for filing in sorted(filings, key=lambda f: f.as_of):
        as_of = filing.as_of
        rows = resolved_by_as_of[as_of]

        conn.execute(
            """
            INSERT INTO etf_dates (etf_id, as_of, holdings_count, total_value_usd, source, accession)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (etf_id, as_of, len(rows), filing.total_value_usd, filing.source, filing.accession),
        )

        cur_map = {r.security_id: r for r in rows}

        holding_tuples: list[tuple[Any, ...]] = []
        for r in rows:
            prev_r = prev_holdings_map.get(r.security_id) if prev_holdings_map is not None else None
            prev_w = prev_r.weight if prev_r is not None else None
            is_new = 1 if (prev_holdings_map is not None and prev_r is None) else 0
            holding_tuples.append(
                (
                    etf_id,
                    as_of,
                    r.security_id,
                    r.ticker,
                    r.reported_name,
                    r.shares,
                    r.value_usd,
                    r.weight,
                    r.pct_net_assets,
                    r.rank,
                    prev_w,
                    is_new,
                )
            )

        conn.executemany(
            """
            INSERT INTO etf_holdings (
                etf_id, as_of, security_id, ticker, reported_name, shares, value_usd,
                weight, pct_net_assets, rank, prev_weight, is_new
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            holding_tuples,
        )

        # 前回基準日との差分から追加・除外を記録
        if prev_holdings_map is not None:
            added_ids = sorted(set(cur_map) - set(prev_holdings_map))
            removed_ids = sorted(set(prev_holdings_map) - set(cur_map))
            change_rows: list[tuple[Any, ...]] = []
            for sid in added_ids:
                r = cur_map[sid]
                change_rows.append((etf_id, as_of, "add", sid, r.ticker, r.reported_name, r.weight))
            for sid in removed_ids:
                r = prev_holdings_map[sid]
                change_rows.append((etf_id, as_of, "remove", sid, r.ticker, r.reported_name, r.weight))
            if change_rows:
                conn.executemany(
                    """
                    INSERT INTO etf_changes (etf_id, as_of, kind, security_id, ticker, name, weight)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    change_rows,
                )

        prev_holdings_map = cur_map

    conn.commit()


def update_fundamentals(
    conn: sqlite3.Connection,
    client: EdgarClient,
    offline_only: bool = False,
) -> tuple[int, int]:
    """CIK を持つ全銘柄の SEC XBRL 財務データ（年次・TTM）を取得して DB に保存する。"""
    targets = conn.execute(
        """
        SELECT security_id, ticker, cik
        FROM securities
        WHERE cik IS NOT NULL AND cik != ''
        ORDER BY security_id
        """
    ).fetchall()

    # 同一 CIK（例: 株式クラス違い CWEN / CWEN.A）は 1 回だけ抽出して共有する
    facts_cache: dict[
        str, tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any] | None]
    ] = {}
    ok_count = 0
    miss_count = 0

    for idx, row in enumerate(targets, start=1):
        sec_id = row["security_id"]
        cik = row["cik"]

        if cik not in facts_cache:
            try:
                raw_facts = client.get_company_facts(cik, offline_only=offline_only)
                ann_rows, splits = extract_annual_financials(raw_facts)
                q_rows = extract_quarterly_financials(raw_facts, ann_rows, splits)
                ttm_row = extract_ttm_financials(raw_facts, ann_rows)
                facts_cache[cik] = (ann_rows, q_rows, ttm_row)
            except RuntimeError as exc:
                # HTTP 404 または offline 時の未キャッシュはスキップし、ネットワーク障害等は即停止する
                msg = str(exc)
                if "HTTP 404" in msg or (offline_only and "offline mode" in msg):
                    facts_cache[cik] = ([], [], None)
                else:
                    raise

        ann_rows, q_rows, ttm_row = facts_cache[cik]
        conn.execute("DELETE FROM financials_annual WHERE security_id = ?", (sec_id,))
        conn.execute("DELETE FROM financials_quarterly WHERE security_id = ?", (sec_id,))
        conn.execute("DELETE FROM financials_ttm WHERE security_id = ?", (sec_id,))

        if not ann_rows:
            miss_count += 1
            continue

        conn.executemany(
            """
            INSERT INTO financials_annual (
                security_id, fy_end, fy_start, filed,
                revenue, gross_profit, operating_income, pretax_income, net_income,
                rd_expense, sga_expense, interest_expense, income_tax,
                eps_diluted, shares_diluted, shares_outstanding,
                operating_cf, investing_cf, financing_cf, capex, fcf, sbc, da_expense,
                dividends_paid, buybacks,
                total_assets, current_assets, total_equity, current_liabilities,
                cash, total_debt, goodwill, intangibles,
                dps_declared, dps_paid, split_factor
            ) VALUES (
                ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?,
                ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?
            )
            """,
            [
                (
                    sec_id,
                    r["fy_end"],
                    r["fy_start"],
                    r["filed"],
                    r["revenue"],
                    r["gross_profit"],
                    r["operating_income"],
                    r["pretax_income"],
                    r["net_income"],
                    r["rd_expense"],
                    r["sga_expense"],
                    r["interest_expense"],
                    r["income_tax"],
                    r["eps_diluted"],
                    r["shares_diluted"],
                    r["shares_outstanding"],
                    r["operating_cf"],
                    r["investing_cf"],
                    r["financing_cf"],
                    r["capex"],
                    r["fcf"],
                    r["sbc"],
                    r["da_expense"],
                    r["dividends_paid"],
                    r["buybacks"],
                    r["total_assets"],
                    r["current_assets"],
                    r["total_equity"],
                    r["current_liabilities"],
                    r["cash"],
                    r["total_debt"],
                    r["goodwill"],
                    r["intangibles"],
                    r["dps_declared"],
                    r["dps_paid"],
                    r["split_factor"],
                )
                for r in ann_rows
            ],
        )

        if q_rows:
            conn.executemany(
                """
                INSERT INTO financials_quarterly (
                    security_id, period_end, period_start, fp,
                    revenue, operating_income, pretax_income, net_income,
                    eps_diluted, operating_cf, fcf
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        sec_id,
                        q["period_end"],
                        q["period_start"],
                        q["fp"],
                        q["revenue"],
                        q["operating_income"],
                        q["pretax_income"],
                        q["net_income"],
                        q["eps_diluted"],
                        q["operating_cf"],
                        q["fcf"],
                    )
                    for q in q_rows
                ],
            )

        if ttm_row is not None:
            conn.execute(
                """
                INSERT INTO financials_ttm (
                    security_id, period_end, revenue_ttm, operating_income_ttm,
                    net_income_ttm, eps_ttm, fcf_ttm, dps_ttm, total_equity, shares_outstanding
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sec_id,
                    ttm_row["period_end"],
                    ttm_row["revenue_ttm"],
                    ttm_row["operating_income_ttm"],
                    ttm_row["net_income_ttm"],
                    ttm_row["eps_ttm"],
                    ttm_row["fcf_ttm"],
                    ttm_row["dps_ttm"],
                    ttm_row["total_equity"],
                    ttm_row["shares_outstanding"],
                ),
            )

        ok_count += 1
        if idx % 50 == 0 or idx == len(targets):
            conn.commit()
            print(f"  XBRL progress: {idx}/{len(targets)} (ok={ok_count}, no_xbrl={miss_count})")

    conn.commit()
    return ok_count, miss_count


def main() -> int:
    parser = argparse.ArgumentParser(description="Build etf-xray SQLite DB and static JSON site")
    parser.add_argument("--skip-etf", action="store_true", help="Skip ETF holdings fetch/resolve")
    parser.add_argument("--skip-xbrl", action="store_true", help="Skip SEC XBRL fundamentals update")
    parser.add_argument("--offline", action="store_true", help="Use cached XBRL files only without network access")
    parser.add_argument("--only-export", action="store_true", help="Only rebuild metrics and export dist/data")
    args = parser.parse_args()

    conn = init_db(DB_PATH)
    client = EdgarClient()

    try:
        if not args.only_export and not args.skip_etf:
            etf_cfgs = load_etf_configs()
            for cfg in etf_cfgs:
                etf_id = cfg["etf_id"]
                print(f"[1/4] Fetching filings for {etf_id}...")
                filings = fetch_etf_filings(client, cfg)
                print(f"  {etf_id}: {len(filings)} quarters ({filings[0].as_of} .. {filings[-1].as_of})")
                resolved_by_as_of, catalog = resolve_all_filings(client, filings)
                unresolved = [s for s in catalog.values() if not s["ticker"]]
                print(
                    f"  {etf_id}: {len(catalog)} unique securities "
                    f"(unresolved tickers={len(unresolved)})"
                )
                save_etf_and_holdings(conn, cfg, filings, resolved_by_as_of, catalog)

            # どの ETF にも属さなくなった孤立 securities を子テーブルごとクリーンアップ
            orphan_sql = """
                SELECT security_id FROM securities
                WHERE security_id NOT IN (SELECT DISTINCT security_id FROM etf_holdings)
            """
            for child_tbl in (
                "stock_metrics",
                "quarter_prices",
                "financials_ttm",
                "financials_quarterly",
                "financials_annual",
            ):
                conn.execute(f"DELETE FROM {child_tbl} WHERE security_id IN ({orphan_sql})")
            conn.execute(f"DELETE FROM securities WHERE security_id IN ({orphan_sql})")
            conn.commit()

        if not args.only_export and not args.skip_xbrl:
            print("[2/4] Updating SEC XBRL fundamentals...")
            ok_c, miss_c = update_fundamentals(conn, client, offline_only=args.offline)
            print(f"  XBRL complete: ok={ok_c}, unavailable={miss_c}")

        print("[3/4] Computing quarter prices and stock metrics...")
        price_n = rebuild_quarter_prices(conn)
        metric_n = rebuild_stock_metrics(conn)
        print(f"  quarter_prices={price_n}, stock_metrics={metric_n}")
    finally:
        conn.close()

    print("[4/4] Exporting dist/data JSON...")
    stats = export_site()
    print(
        f"  Exported: {stats['etfs']} ETF(s), "
        f"{stats['holdings_files']} holdings files, {stats['stock_files']} stock files"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
