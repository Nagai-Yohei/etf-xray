"""Incremental refresh orchestrator for periodic etf-xray updates.

Detects newly filed or amended N-PORT-P quarters without re-parsing immutable
2011-2019 legacy HTML filings, refreshes stale SEC XBRL CompanyFacts for active
and newly added securities, enforces unresolved-ticker and integrity safety
gates, and skips rebuild/export/deploy when no upstream SEC changes exist.

Examples:
    python -m src.refresh                         # Incremental check & rebuild if changed
    python -m src.refresh --deploy                # Incremental update + check + deploy if changed
    python -m src.refresh --xbrl-max-age-days 14  # Refresh active XBRL older than 14 days
    python -m src.refresh --offline               # Dry-run using local cache only
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

from src.adapters.nport import FilingHoldings, fetch_new_etf_nport_filings
from src.check import main as run_check_main
from src.db import init_db
from src.deploy import deploy as run_deploy
from src.edgar import EdgarClient
from src.env import DATA_DIR, DB_PATH
from src.export_site import export_site
from src.fundamentals import (
    extract_annual_financials,
    extract_quarterly_financials,
    extract_ttm_financials,
    get_cik_chain,
    load_cik_overrides,
    merge_company_facts,
)
from src.identify import ResolvedHolding, is_non_common_ticker, resolve_all_filings
from src.metrics import rebuild_quarter_prices, rebuild_stock_metrics
from src.pipeline import load_etf_configs
from src.sectors import classify_sector_group, normalize_sic

LOGS_DIR = DATA_DIR / "logs"


@dataclass
class FilingDiff:
    """Summary of a newly ingested or amended ETF reporting date."""

    etf_id: str
    as_of: str
    action: str  # "added" or "amended"
    accession: str
    holdings_count: int
    added_tickers: list[str] = field(default_factory=list)
    removed_tickers: list[str] = field(default_factory=list)


@dataclass
class RefreshSummary:
    """Structured summary of an incremental refresh run."""

    started_at: str
    finished_at: str = ""
    etfs_checked: int = 0
    filing_diffs: list[FilingDiff] = field(default_factory=list)
    xbrl_checked: int = 0
    xbrl_updated: int = 0
    xbrl_missing: int = 0
    sic_updated: int = 0
    exported: bool = False
    export_stats: dict[str, Any] | None = None
    check_passed: bool | None = None
    deployed: bool = False
    unresolved_items: list[dict[str, str]] = field(default_factory=list)
    aborted_reason: str | None = None

    @property
    def has_changes(self) -> bool:
        return bool(
            self.filing_diffs
            or self.xbrl_updated > 0
            or self.sic_updated > 0
        )


def rebuild_etf_history(
    conn: sqlite3.Connection,
    etf_id: str,
) -> dict[str, tuple[list[str], list[str]]]:
    """Recompute prev_weight, is_new, and etf_changes for one ETF from SQLite holdings.

    Returns a mapping of `as_of -> (added_tickers, removed_tickers)` for each date
    after the first reporting date.
    """
    dates = [
        r["as_of"]
        for r in conn.execute(
            "SELECT as_of FROM etf_dates WHERE etf_id = ? ORDER BY as_of ASC",
            (etf_id,),
        ).fetchall()
    ]

    conn.execute("DELETE FROM etf_changes WHERE etf_id = ?", (etf_id,))

    diffs_by_date: dict[str, tuple[list[str], list[str]]] = {}
    prev_map: dict[str, dict[str, Any]] | None = None

    for as_of in dates:
        rows = conn.execute(
            """
            SELECT security_id, ticker, reported_name, weight
            FROM etf_holdings
            WHERE etf_id = ? AND as_of = ?
            """,
            (etf_id, as_of),
        ).fetchall()
        cur_map = {
            r["security_id"]: {
                "ticker": r["ticker"],
                "reported_name": r["reported_name"],
                "weight": r["weight"],
            }
            for r in rows
        }

        updates: list[tuple[float | None, int, str, str, str]] = []
        for sid in cur_map:
            prev_row = prev_map.get(sid) if prev_map is not None else None
            prev_w = prev_row["weight"] if prev_row is not None else None
            is_new = 1 if (prev_map is not None and prev_row is None) else 0
            updates.append((prev_w, is_new, etf_id, as_of, sid))

        if updates:
            conn.executemany(
                """
                UPDATE etf_holdings
                SET prev_weight = ?, is_new = ?
                WHERE etf_id = ? AND as_of = ? AND security_id = ?
                """,
                updates,
            )

        if prev_map is not None:
            added_ids = sorted(set(cur_map) - set(prev_map))
            removed_ids = sorted(set(prev_map) - set(cur_map))
            change_rows: list[tuple[Any, ...]] = []
            added_tickers: list[str] = []
            removed_tickers: list[str] = []

            for sid in added_ids:
                info = cur_map[sid]
                change_rows.append(
                    (etf_id, as_of, "add", sid, info["ticker"], info["reported_name"], info["weight"])
                )
                added_tickers.append(info["ticker"] or sid)
            for sid in removed_ids:
                info = prev_map[sid]
                change_rows.append(
                    (etf_id, as_of, "remove", sid, info["ticker"], info["reported_name"], info["weight"])
                )
                removed_tickers.append(info["ticker"] or sid)

            if change_rows:
                conn.executemany(
                    """
                    INSERT INTO etf_changes (etf_id, as_of, kind, security_id, ticker, name, weight)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    change_rows,
                )
            diffs_by_date[as_of] = (added_tickers, removed_tickers)
        else:
            diffs_by_date[as_of] = ([], [])

        prev_map = cur_map

    return diffs_by_date


def upsert_etf_holdings_incremental(
    conn: sqlite3.Connection,
    etf_cfg: dict[str, Any],
    filings: list[FilingHoldings],
    resolved_by_as_of: dict[str, list[ResolvedHolding]],
    securities_catalog: dict[str, dict[str, Any]],
) -> list[FilingDiff]:
    """Upsert new/amended N-PORT-P dates for an ETF while preserving historical rows."""
    if not filings:
        return []

    etf_id = etf_cfg["etf_id"]

    # Track which dates already existed so we can classify "added" vs "amended"
    existing_as_ofs = {
        r["as_of"]
        for r in conn.execute(
            "SELECT as_of FROM etf_dates WHERE etf_id = ?",
            (etf_id,),
        ).fetchall()
    }

    # 1. Upsert etfs metadata
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

    # 2. Upsert securities (preserving existing SIC metadata and merging first_seen/last_seen)
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

    # 3. Replace only the newly fetched or amended (etf_id, as_of) slices
    for filing in sorted(filings, key=lambda f: f.as_of):
        as_of = filing.as_of
        rows = resolved_by_as_of[as_of]

        conn.execute(
            "DELETE FROM etf_holdings WHERE etf_id = ? AND as_of = ?",
            (etf_id, as_of),
        )
        conn.execute(
            "DELETE FROM etf_dates WHERE etf_id = ? AND as_of = ?",
            (etf_id, as_of),
        )

        conn.execute(
            """
            INSERT INTO etf_dates (etf_id, as_of, holdings_count, total_value_usd, source, accession)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (etf_id, as_of, len(rows), filing.total_value_usd, filing.source, filing.accession),
        )

        conn.executemany(
            """
            INSERT INTO etf_holdings (
                etf_id, as_of, security_id, ticker, reported_name, shares, value_usd,
                weight, pct_net_assets, rank, prev_weight, is_new
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0)
            """,
            [
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
                )
                for r in rows
            ],
        )

    # 4. Recompute prev_weight, is_new, and etf_changes across the ETF's timeline
    diffs_by_date = rebuild_etf_history(conn, etf_id)
    conn.commit()

    filing_diffs: list[FilingDiff] = []
    for filing in sorted(filings, key=lambda f: f.as_of):
        added_t, removed_t = diffs_by_date.get(filing.as_of, ([], []))
        filing_diffs.append(
            FilingDiff(
                etf_id=etf_id,
                as_of=filing.as_of,
                action="amended" if filing.as_of in existing_as_ofs else "added",
                accession=filing.accession,
                holdings_count=len(resolved_by_as_of[filing.as_of]),
                added_tickers=added_t,
                removed_tickers=removed_t,
            )
        )
    return filing_diffs


def _apply_cik_hygiene(
    conn: sqlite3.Connection,
    cik_overrides: dict[str, dict[str, Any]],
    ticker_to_cik: dict[str, str] | None = None,
) -> None:
    """Enforce CIK overrides, backfill newly mapped tickers, and clear non-common/.OLD CIKs."""
    # Backfill missing CIKs from refreshed SEC company_tickers.json where appropriate
    if ticker_to_cik:
        missing_rows = conn.execute(
            """
            SELECT security_id, ticker
            FROM securities
            WHERE (cik IS NULL OR cik = '') AND ticker IS NOT NULL AND ticker != ''
            """
        ).fetchall()
        for row in missing_rows:
            sec_id = row["security_id"]
            if sec_id in cik_overrides or is_non_common_ticker(sec_id) or sec_id.endswith(".OLD"):
                continue
            tkr = str(row["ticker"]).upper()
            mapped_cik = ticker_to_cik.get(tkr) or ticker_to_cik.get(tkr.replace(".", "-"))
            if mapped_cik:
                conn.execute(
                    "UPDATE securities SET cik = ? WHERE security_id = ?",
                    (mapped_cik, sec_id),
                )

    for sec_id, entry in cik_overrides.items():
        override_cik = entry.get("cik")
        if override_cik:
            conn.execute(
                "UPDATE securities SET cik = ? WHERE security_id = ?",
                (override_cik, sec_id),
            )
        else:
            conn.execute(
                "UPDATE securities SET cik = NULL WHERE security_id = ?",
                (sec_id,),
            )

    all_sec_ids = [
        r["security_id"]
        for r in conn.execute(
            "SELECT security_id FROM securities WHERE cik IS NOT NULL AND cik != ''"
        ).fetchall()
    ]
    for sec_id in all_sec_ids:
        if is_non_common_ticker(sec_id) or sec_id.endswith(".OLD"):
            conn.execute("UPDATE securities SET cik = NULL WHERE security_id = ?", (sec_id,))

    for tbl in ("financials_annual", "financials_quarterly", "financials_ttm"):
        conn.execute(
            f"""
            DELETE FROM {tbl}
            WHERE security_id IN (
                SELECT security_id FROM securities WHERE cik IS NULL OR cik = ''
            )
            """
        )
    conn.commit()


def _write_security_financials(
    conn: sqlite3.Connection,
    sec_id: str,
    ann_rows: list[dict[str, Any]],
    q_rows: list[dict[str, Any]],
    ttm_row: dict[str, Any] | None,
) -> None:
    """Replace annual, quarterly, and TTM XBRL rows for one security."""
    conn.execute("DELETE FROM financials_annual WHERE security_id = ?", (sec_id,))
    conn.execute("DELETE FROM financials_quarterly WHERE security_id = ?", (sec_id,))
    conn.execute("DELETE FROM financials_ttm WHERE security_id = ?", (sec_id,))

    if not ann_rows:
        return

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


def update_fundamentals_incremental(
    conn: sqlite3.Connection,
    client: EdgarClient,
    *,
    xbrl_max_age_days: float = 14.0,
    xbrl_limit: int = 0,
    tickers_max_age_days: float = 7.0,
    offline_only: bool = False,
) -> tuple[int, int, int]:
    """Refresh stale CompanyFacts for active holdings and newly added securities.

    When `xbrl_limit > 0`, caps the number of stale CIK chains network-refreshed
    in a single run (prioritizing missing / oldest-cached chains first).

    Returns (checked_count, updated_count, missing_count).
    """
    cik_overrides = load_cik_overrides()
    ticker_to_cik: dict[str, str] | None = None
    try:
        ticker_to_cik = client.build_ticker_to_cik_map(
            max_age_days=tickers_max_age_days,
            offline_only=offline_only,
        )
    except RuntimeError as exc:
        if not (offline_only and "offline mode" in str(exc)):
            raise

    _apply_cik_hygiene(conn, cik_overrides, ticker_to_cik=ticker_to_cik)

    targets = conn.execute(
        """
        SELECT DISTINCT
            s.security_id,
            s.ticker,
            s.cik,
            CASE WHEN EXISTS (
                SELECT 1 FROM financials_annual fa WHERE fa.security_id = s.security_id
            ) THEN 1 ELSE 0 END AS has_annual
        FROM securities s
        WHERE s.cik IS NOT NULL AND s.cik != ''
          AND (
              s.security_id IN (
                  SELECT DISTINCT h.security_id
                  FROM etf_holdings h
                  JOIN (
                      SELECT etf_id, MAX(as_of) AS max_as_of
                      FROM etf_dates
                      GROUP BY etf_id
                  ) latest
                    ON h.etf_id = latest.etf_id AND h.as_of = latest.max_as_of
              )
              OR NOT EXISTS (
                  SELECT 1 FROM financials_annual fa WHERE fa.security_id = s.security_id
              )
          )
        ORDER BY s.security_id
        """
    ).fetchall()

    # If xbrl_limit > 0, pick up to `xbrl_limit` oldest stale CIK chains for network refresh
    network_allowed_chains: set[tuple[str, ...]] | None = None
    if xbrl_limit > 0 and not offline_only:
        cutoff_ts = time.time() - (max(xbrl_max_age_days, 0.0) * 86400.0)
        stale_chains: list[tuple[float, tuple[str, ...]]] = []
        seen_chains: set[tuple[str, ...]] = set()
        for row in targets:
            chain = get_cik_chain(row["security_id"], row["cik"], cik_overrides)
            if chain in seen_chains:
                continue
            seen_chains.add(chain)
            oldest_mtime = min(client.get_company_facts_mtime(c) for c in chain)
            if xbrl_max_age_days <= 0 or oldest_mtime < cutoff_ts:
                stale_chains.append((oldest_mtime, chain))
        stale_chains.sort(key=lambda item: (item[0], item[1]))
        network_allowed_chains = {chain for _, chain in stale_chains[:xbrl_limit]}

    # Cache refresh status (available_ciks, chain_changed) and parsed tuples per CIK chain
    chain_status_cache: dict[tuple[str, ...], tuple[list[str], bool]] = {}
    parsed_cache: dict[
        tuple[str, ...], tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any] | None]
    ] = {}

    checked_count = 0
    updated_count = 0
    missing_count = 0

    for idx, row in enumerate(targets, start=1):
        sec_id = row["security_id"]
        cik = row["cik"]
        has_annual = bool(row["has_annual"])
        cik_chain = get_cik_chain(sec_id, cik, cik_overrides)
        checked_count += 1

        if cik_chain not in chain_status_cache:
            use_offline = offline_only or (
                network_allowed_chains is not None and cik_chain not in network_allowed_chains
            )
            available_ciks: list[str] = []
            chain_changed = False
            for c in cik_chain:
                try:
                    changed = client.refresh_company_facts(
                        c,
                        offline_only=use_offline,
                        max_age_days=xbrl_max_age_days,
                    )
                    available_ciks.append(c)
                    if changed:
                        chain_changed = True
                except RuntimeError as exc:
                    msg = str(exc)
                    if "HTTP 404" in msg or (use_offline and "offline mode" in msg):
                        continue
                    raise
            chain_status_cache[cik_chain] = (available_ciks, chain_changed)

        available_ciks, chain_changed = chain_status_cache[cik_chain]
        if not available_ciks:
            missing_count += 1
            continue

        # Skip reading/decoding JSON when cached bytes did not change and DB already has rows
        if not chain_changed and has_annual:
            continue

        if cik_chain not in parsed_cache:
            payloads = [client.get_company_facts(c, offline_only=True) for c in available_ciks]
            merged_facts = merge_company_facts(payloads)
            ann_rows, splits = extract_annual_financials(merged_facts)
            q_rows = extract_quarterly_financials(merged_facts, ann_rows, splits)
            ttm_row = extract_ttm_financials(merged_facts, ann_rows)
            parsed_cache[cik_chain] = (ann_rows, q_rows, ttm_row)

        ann_rows, q_rows, ttm_row = parsed_cache[cik_chain]
        if not ann_rows:
            missing_count += 1
            continue

        _write_security_financials(conn, sec_id, ann_rows, q_rows, ttm_row)
        updated_count += 1

        if updated_count % 50 == 0:
            conn.commit()
        if idx % 100 == 0 or idx == len(targets):
            print(
                f"  Incremental XBRL progress: {idx}/{len(targets)} "
                f"(updated={updated_count}, no_xbrl={missing_count})"
            )

    conn.commit()
    return checked_count, updated_count, missing_count


def update_sic_incremental(
    conn: sqlite3.Connection,
    client: EdgarClient,
    *,
    offline_only: bool = False,
) -> int:
    """Populate SIC sector metadata for securities missing SIC."""
    conn.execute(
        """
        UPDATE securities
        SET sic = NULL, sic_description = NULL, sector_group = NULL
        WHERE cik IS NULL OR cik = ''
        """
    )

    ciks = [
        r["cik"]
        for r in conn.execute(
            """
            SELECT DISTINCT cik
            FROM securities
            WHERE cik IS NOT NULL AND cik != '' AND sic IS NULL
            ORDER BY cik
            """
        ).fetchall()
    ]
    if not ciks:
        conn.commit()
        return 0

    updated = 0
    for idx, cik in enumerate(ciks, start=1):
        try:
            sub = client.get_submissions(cik, offline_only=offline_only)
        except RuntimeError as exc:
            msg = str(exc)
            if "HTTP 404" in msg or (offline_only and "offline mode" in msg):
                continue
            raise

        sic = normalize_sic(sub.get("sic"))
        sic_desc = str(sub.get("sicDescription") or "").strip() or None
        group = classify_sector_group(sic, sic_desc)
        if sic is not None or sic_desc is not None or group is not None:
            conn.execute(
                """
                UPDATE securities
                SET sic = ?, sic_description = ?, sector_group = ?
                WHERE cik = ?
                """,
                (sic, sic_desc, group, cik),
            )
            updated += 1
        if idx % 200 == 0 or idx == len(ciks):
            conn.commit()
            print(f"  Incremental SIC progress: {idx}/{len(ciks)} (updated={updated})", flush=True)

    conn.commit()
    return updated


def format_summary_report(summary: RefreshSummary) -> str:
    """Format a concise human-readable refresh summary for console and log files."""
    lines = [
        "=== etf-xray incremental refresh ===",
        f"Started:  {summary.started_at}",
        f"Finished: {summary.finished_at}",
        f"ETFs checked: {summary.etfs_checked}",
        f"New/amended N-PORT-P filings: {len(summary.filing_diffs)}",
    ]
    for diff in summary.filing_diffs:
        add_str = ", ".join(diff.added_tickers[:10])
        if len(diff.added_tickers) > 10:
            add_str += f" (+{len(diff.added_tickers) - 10} more)"
        rem_str = ", ".join(diff.removed_tickers[:10])
        if len(diff.removed_tickers) > 10:
            rem_str += f" (+{len(diff.removed_tickers) - 10} more)"
        lines.append(
            f"  - {diff.etf_id} {diff.as_of} ({diff.action}, {diff.holdings_count} holdings, {diff.accession})"
        )
        if diff.added_tickers:
            lines.append(f"      + Added ({len(diff.added_tickers)}): {add_str}")
        if diff.removed_tickers:
            lines.append(f"      - Removed ({len(diff.removed_tickers)}): {rem_str}")

    lines.append(
        f"XBRL active/new checked: {summary.xbrl_checked} "
        f"(updated={summary.xbrl_updated}, unavailable={summary.xbrl_missing})"
    )
    lines.append(f"SIC newly populated: {summary.sic_updated}")

    if summary.unresolved_items:
        lines.append(f"UNRESOLVED TICKERS ({len(summary.unresolved_items)}) - ABORTED:")
        for item in summary.unresolved_items:
            lines.append(
                f"  - {item['etf_id']} {item['as_of']}: "
                f"cusip={item['cusip'] or 'N/A'} isin={item['isin'] or 'N/A'} "
                f"name={item['reported_name']!r}"
            )
        lines.append("Action: add missing entries to config/manual_tickers.csv and re-run.")

    if summary.aborted_reason:
        lines.append(f"Status: ABORTED ({summary.aborted_reason})")
    elif summary.exported:
        stats = summary.export_stats or {}
        lines.append(
            f"Exported dist/: {stats.get('etfs', '?')} ETFs, "
            f"{stats.get('holdings_files', '?')} holdings files, "
            f"{stats.get('stock_files', '?')} stock files"
        )
        if summary.check_passed is not None:
            lines.append(f"Integrity check: {'PASS' if summary.check_passed else 'FAIL'}")
        lines.append(f"Deployed: {'YES' if summary.deployed else 'NO'}")
    else:
        lines.append("Status: NO CHANGES (skipped metrics rebuild, export, and deploy)")

    return "\n".join(lines) + "\n"


def write_refresh_log(report_text: str, log_path: Path | None = None) -> Path:
    """Append the refresh summary to data/logs/refresh-YYYY-MM-DD.log."""
    if log_path is None:
        today = datetime.now().strftime("%Y-%m-%d")
        log_path = LOGS_DIR / f"refresh-{today}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(report_text)
        fh.write("\n")
    return log_path


def run_incremental_refresh(
    *,
    db_path: Path = DB_PATH,
    client: EdgarClient | None = None,
    etf_filter: set[str] | None = None,
    etf_max_age_days: float = 0.5,
    xbrl_max_age_days: float = 14.0,
    xbrl_limit: int = 0,
    tickers_max_age_days: float = 7.0,
    skip_etf: bool = False,
    skip_xbrl: bool = False,
    offline_only: bool = False,
    force_export: bool = False,
    allow_unresolved: bool = False,
    run_check: bool = True,
    deploy: bool = False,
    log_path: Path | None = None,
    write_log: bool = True,
) -> tuple[int, RefreshSummary]:
    """Execute the incremental refresh workflow and return (exit_code, RefreshSummary)."""
    started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    summary = RefreshSummary(started_at=started)

    edgar = client or EdgarClient()
    conn = init_db(db_path)

    try:
        # 1. Incremental N-PORT-P discovery and resolution
        if not skip_etf:
            etf_cfgs = load_etf_configs()
            if etf_filter:
                etf_cfgs = [c for c in etf_cfgs if c["etf_id"].upper() in etf_filter]
            summary.etfs_checked = len(etf_cfgs)

            staged_updates: list[
                tuple[
                    dict[str, Any],
                    list[FilingHoldings],
                    dict[str, list[ResolvedHolding]],
                    dict[str, dict[str, Any]],
                ]
            ] = []

            for cfg in etf_cfgs:
                etf_id = cfg["etf_id"]
                existing_dates = {
                    r["as_of"]: (r["accession"] or "")
                    for r in conn.execute(
                        "SELECT as_of, accession FROM etf_dates WHERE etf_id = ?",
                        (etf_id,),
                    ).fetchall()
                }
                print(f"[1/4] Checking {etf_id} for new N-PORT-P filings...")
                new_filings = fetch_new_etf_nport_filings(
                    edgar,
                    cfg,
                    existing_dates,
                    max_age_days=etf_max_age_days,
                    offline_only=offline_only,
                )
                if not new_filings:
                    print(f"  {etf_id}: up to date ({len(existing_dates)} dates stored)")
                    continue

                dates_str = ", ".join(f.as_of for f in new_filings)
                print(f"  {etf_id}: {len(new_filings)} new/amended N-PORT-P filing(s): {dates_str}")
                resolved_by_as_of, catalog = resolve_all_filings(
                    edgar,
                    new_filings,
                    tickers_max_age_days=tickers_max_age_days,
                    offline_only=offline_only,
                )

                for as_of, rows in resolved_by_as_of.items():
                    for r in rows:
                        if not r.ticker:
                            summary.unresolved_items.append(
                                {
                                    "etf_id": etf_id,
                                    "as_of": as_of,
                                    "cusip": r.cusip or "",
                                    "isin": r.isin or "",
                                    "reported_name": r.reported_name,
                                }
                            )

                staged_updates.append((cfg, new_filings, resolved_by_as_of, catalog))

            # Safety gate: abort before mutating SQLite if any new holding is unresolved
            if summary.unresolved_items and not allow_unresolved:
                summary.aborted_reason = (
                    f"{len(summary.unresolved_items)} unresolved ticker(s) in new N-PORT-P filings"
                )
                summary.finished_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                report = format_summary_report(summary)
                print(report, file=sys.stderr)
                if write_log:
                    written = write_refresh_log(report, log_path=log_path)
                    print(f"Log written to {written}", file=sys.stderr)
                return 2, summary

            # Commit staged ETF updates cleanly
            for cfg, new_filings, resolved_by_as_of, catalog in staged_updates:
                diffs = upsert_etf_holdings_incremental(
                    conn, cfg, new_filings, resolved_by_as_of, catalog
                )
                summary.filing_diffs.extend(diffs)

        # 2. Incremental XBRL & SIC updates for active and newly added securities
        if not skip_xbrl:
            print(
                f"[2/4] Checking active/new XBRL CompanyFacts "
                f"(max_age_days={xbrl_max_age_days:g}) & missing SIC..."
            )
            checked, updated, missing = update_fundamentals_incremental(
                conn,
                edgar,
                xbrl_max_age_days=xbrl_max_age_days,
                xbrl_limit=xbrl_limit,
                tickers_max_age_days=tickers_max_age_days,
                offline_only=offline_only,
            )
            summary.xbrl_checked = checked
            summary.xbrl_updated = updated
            summary.xbrl_missing = missing
            summary.sic_updated = update_sic_incremental(
                conn,
                edgar,
                offline_only=offline_only,
            )
            print(
                f"  XBRL: checked={checked}, updated={updated}, unavailable={missing}; "
                f"SIC newly populated={summary.sic_updated}"
            )

        # 3. Short-circuit when nothing changed
        if not summary.has_changes and not force_export:
            summary.finished_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            report = format_summary_report(summary)
            print(report)
            if write_log:
                written = write_refresh_log(report, log_path=log_path)
                print(f"Log written to {written}")
            return 0, summary

        # 4. Rebuild metrics
        print("[3/4] Rebuilding quarter prices and stock metrics...")
        price_n = rebuild_quarter_prices(conn)
        metric_n = rebuild_stock_metrics(conn)
        print(f"  quarter_prices={price_n}, stock_metrics={metric_n}")
    finally:
        conn.close()

    # 5. Export static JSON site
    print("[4/4] Exporting dist/data JSON...")
    summary.export_stats = export_site(db_path=db_path)
    summary.exported = True
    print(
        f"  Exported: {summary.export_stats['etfs']} ETF(s), "
        f"{summary.export_stats['holdings_files']} holdings files, "
        f"{summary.export_stats['stock_files']} stock files"
    )

    # 6. Run integrity check (always required before deploy)
    if run_check or deploy:
        print("[Check] Running integrity checks, secret scan, and unit tests...")
        check_rc = run_check_main()
        summary.check_passed = check_rc == 0
        if check_rc != 0:
            summary.aborted_reason = "src.check failed; deploy skipped"
            summary.finished_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            report = format_summary_report(summary)
            print(report, file=sys.stderr)
            if write_log:
                written = write_refresh_log(report, log_path=log_path)
                print(f"Log written to {written}", file=sys.stderr)
            return check_rc, summary

    # 7. Optional Cloudflare Pages deploy
    if deploy:
        print("[Deploy] Uploading dist/ to Cloudflare Pages...")
        try:
            deploy_rc = run_deploy(dry_run=False, skip_check=True)
        except RuntimeError as exc:
            print(f"DEPLOY FAILED: {exc}", file=sys.stderr)
            deploy_rc = 1
        if deploy_rc != 0:
            summary.aborted_reason = f"src.deploy exited with code {deploy_rc}"
            summary.finished_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            report = format_summary_report(summary)
            print(report, file=sys.stderr)
            if write_log:
                written = write_refresh_log(report, log_path=log_path)
                print(f"Log written to {written}", file=sys.stderr)
            return deploy_rc, summary
        summary.deployed = True

    summary.finished_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    report = format_summary_report(summary)
    print(report)
    if write_log:
        written = write_refresh_log(report, log_path=log_path)
        print(f"Log written to {written}")
    return 0, summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Incremental SEC EDGAR refresh for etf-xray"
    )
    parser.add_argument(
        "--incremental",
        action="store_true",
        help="Explicit incremental mode flag (default behavior of src.refresh)",
    )
    parser.add_argument(
        "--etf",
        action="append",
        default=[],
        help="Limit ETF check to specific ETF ID(s) (repeatable or comma-separated)",
    )
    parser.add_argument(
        "--etf-max-age-days",
        type=float,
        default=0.5,
        help="Max cache age in days for ETF submissions & EFTS lookups (default: 0.5)",
    )
    parser.add_argument(
        "--xbrl-max-age-days",
        type=float,
        default=14.0,
        help="Max cache age in days for active security CompanyFacts (default: 14)",
    )
    parser.add_argument(
        "--xbrl-limit",
        type=int,
        default=0,
        help="Max stale CIK chains to network-refresh per run, oldest first (0 = unlimited)",
    )
    parser.add_argument(
        "--tickers-max-age-days",
        type=float,
        default=7.0,
        help="Max cache age in days for SEC company_tickers.json (default: 7)",
    )
    parser.add_argument("--skip-etf", action="store_true", help="Skip N-PORT-P ETF check")
    parser.add_argument("--skip-xbrl", action="store_true", help="Skip XBRL & SIC check")
    parser.add_argument("--offline", action="store_true", help="Use local cache only without network")
    parser.add_argument(
        "--force-export",
        action="store_true",
        help="Rebuild metrics and export dist/ even when 0 upstream changes were detected",
    )
    parser.add_argument(
        "--allow-unresolved",
        action="store_true",
        help="Allow committing new N-PORT-P filings even if unresolved tickers appear",
    )
    parser.add_argument(
        "--no-check",
        action="store_true",
        help="Skip src.check after export (ignored when --deploy is passed)",
    )
    parser.add_argument(
        "--deploy",
        action="store_true",
        help="Deploy dist/ to Cloudflare Pages if changes occurred and src.check passes",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        default=None,
        help="Custom log file path (default: data/logs/refresh-YYYY-MM-DD.log)",
    )
    args = parser.parse_args(argv)

    etf_filter: set[str] | None = None
    if args.etf:
        etf_filter = {
            part.strip().upper()
            for item in args.etf
            for part in item.split(",")
            if part.strip()
        }

    rc, _ = run_incremental_refresh(
        etf_filter=etf_filter,
        etf_max_age_days=args.etf_max_age_days,
        xbrl_max_age_days=args.xbrl_max_age_days,
        xbrl_limit=args.xbrl_limit,
        tickers_max_age_days=args.tickers_max_age_days,
        skip_etf=args.skip_etf,
        skip_xbrl=args.skip_xbrl,
        offline_only=args.offline,
        force_export=args.force_export,
        allow_unresolved=args.allow_unresolved,
        run_check=not args.no_check,
        deploy=args.deploy,
        log_path=args.log_file,
    )
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
