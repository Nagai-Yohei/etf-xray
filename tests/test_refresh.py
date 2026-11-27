"""Unit tests for TTL caching, incremental N-PORT-P discovery, and refresh safety gates."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.adapters.nport import FilingHoldings, RawHolding, fetch_new_etf_nport_filings
from src.db import init_db
from src.edgar import EdgarClient
from src.identify import ResolvedHolding
from src.refresh import (
    format_summary_report,
    run_incremental_refresh,
    upsert_etf_holdings_incremental,
)


def _make_nport_xml(series_id: str, rep_date: str, cusip: str, name: str, val: float = 1000.0) -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<edgarSubmission xmlns="http://www.sec.gov/edgar/nport">
  <formData>
    <genInfo>
      <seriesId>{series_id}</seriesId>
      <repPdDate>{rep_date}</repPdDate>
    </genInfo>
    <invstOrSecs>
      <invstOrSec>
        <name>{name}</name>
        <cusip>{cusip}</cusip>
        <balance>10</balance>
        <valUSD>{val}</valUSD>
        <pctVal>100.0</pctVal>
        <payoffProfile>Long</payoffProfile>
        <assetCat>EC</assetCat>
      </invstOrSec>
    </invstOrSecs>
  </formData>
</edgarSubmission>
""".encode("utf-8")


class TestEdgarCacheTTL(unittest.TestCase):
    def test_fresh_expired_unchanged_and_changed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = EdgarClient(cache_dir=Path(tmp), min_interval=0.2)
            client._wait_rate_limit = MagicMock()  # type: ignore[method-assign]

            fake_session = MagicMock()
            resp1 = MagicMock(status_code=200, content=b'{"v": 1}')
            fake_session.get.return_value = resp1
            client._session = fake_session

            # Initial download -> changed=True
            data, changed = client.fetch_bytes_with_status(
                "https://data.sec.gov/test.json", "test.json", max_age_days=14.0
            )
            self.assertEqual(data, b'{"v": 1}')
            self.assertTrue(changed)
            self.assertEqual(fake_session.get.call_count, 1)

            # Fresh cache hit -> no network, changed=False
            data2, changed2 = client.fetch_bytes_with_status(
                "https://data.sec.gov/test.json", "test.json", max_age_days=14.0
            )
            self.assertEqual(data2, b'{"v": 1}')
            self.assertFalse(changed2)
            self.assertEqual(fake_session.get.call_count, 1)

            # Backdate mtime by 20 days -> expired, same bytes -> network hit, changed=False
            cache_file = Path(tmp) / "test.json"
            old_ts = time.time() - (20 * 86400)
            os.utime(cache_file, (old_ts, old_ts))

            data3, changed3 = client.fetch_bytes_with_status(
                "https://data.sec.gov/test.json", "test.json", max_age_days=14.0
            )
            self.assertEqual(data3, b'{"v": 1}')
            self.assertFalse(changed3)
            self.assertEqual(fake_session.get.call_count, 2)
            # mtime was refreshed so immediate follow-up is a cache hit again
            self.assertGreater(cache_file.stat().st_mtime, old_ts + 1000)

            # Backdate again and return modified bytes -> changed=True
            os.utime(cache_file, (old_ts, old_ts))
            fake_session.get.return_value = MagicMock(status_code=200, content=b'{"v": 2}')
            data4, changed4 = client.fetch_bytes_with_status(
                "https://data.sec.gov/test.json", "test.json", max_age_days=14.0
            )
            self.assertEqual(data4, b'{"v": 2}')
            self.assertTrue(changed4)
            self.assertEqual(fake_session.get.call_count, 3)

            # Offline mode uses expired cache without network
            os.utime(cache_file, (old_ts, old_ts))
            data5, changed5 = client.fetch_bytes_with_status(
                "https://data.sec.gov/test.json",
                "test.json",
                max_age_days=1.0,
                offline_only=True,
            )
            self.assertEqual(data5, b'{"v": 2}')
            self.assertFalse(changed5)
            self.assertEqual(fake_session.get.call_count, 3)

    def test_get_all_filings_skips_old_archive_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            sub_dir = cache_dir / "submissions"
            sub_dir.mkdir(parents=True)

            (sub_dir / "CIK0001100663.json").write_text(
                json.dumps(
                    {
                        "filings": {
                            "recent": {
                                "accessionNumber": ["0001-26-000001"],
                                "form": ["NPORT-P"],
                                "filingDate": ["2026-09-25"],
                                "reportDate": ["2026-07-31"],
                                "primaryDocument": ["primary_doc.xml"],
                            },
                            "files": [
                                {
                                    "name": "CIK0001100663-submissions-001.json",
                                    "filingFrom": "2024-01-01",
                                    "filingTo": "2024-12-31",
                                }
                            ],
                        }
                    }
                ),
                encoding="utf-8",
            )
            # Deliberately omit CIK0001100663-submissions-001.json; offline mode would fail if fetched
            client = EdgarClient(cache_dir=cache_dir)
            rows = client.get_all_filings(
                "0001100663",
                forms={"NPORT-P"},
                min_filing_date="2025-07-31",
                offline_only=True,
            )
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["accessionNumber"], "0001-26-000001")


class TestIncrementalNportDiscovery(unittest.TestCase):
    def test_discovers_new_quarter_and_amendment_without_downgrading(self) -> None:
        etf_cfg = {
            "etf_id": "TEST",
            "name": "Test ETF",
            "cik": "0001041130",
            "series_id": None,
        }
        client = MagicMock(spec=EdgarClient)
        client.get_all_filings.return_value = [
            # Already stored quarter (original)
            {
                "accessionNumber": "0001-26-000010",
                "form": "NPORT-P",
                "filingDate": "2026-05-20",
                "reportDate": "2026-03-31",
                "primaryDocument": "primary_doc.xml",
            },
            # Superseded original for 2026-06-30 (DB already holds amendment 000021)
            {
                "accessionNumber": "0001-26-000020",
                "form": "NPORT-P",
                "filingDate": "2026-08-20",
                "reportDate": "2026-06-30",
                "primaryDocument": "primary_doc.xml",
            },
            # Stored amendment for 2026-06-30
            {
                "accessionNumber": "0001-26-000021",
                "form": "NPORT-P/A",
                "filingDate": "2026-08-25",
                "reportDate": "2026-06-30",
                "primaryDocument": "primary_doc.xml",
            },
            # Newer amendment for 2026-03-31 -> SHOULD be fetched
            {
                "accessionNumber": "0001-26-000015",
                "form": "NPORT-P/A",
                "filingDate": "2026-09-01",
                "reportDate": "2026-03-31",
                "primaryDocument": "primary_doc.xml",
            },
            # Brand-new quarter 2026-09-30 -> SHOULD be fetched
            {
                "accessionNumber": "0001-26-000030",
                "form": "NPORT-P",
                "filingDate": "2026-11-20",
                "reportDate": "2026-09-30",
                "primaryDocument": "primary_doc.xml",
            },
        ]

        xml_by_accn = {
            "0001-26-000015": _make_nport_xml("S000000001", "2026-03-31", "037833100", "Apple Inc"),
            "0001-26-000030": _make_nport_xml("S000000001", "2026-09-30", "594918104", "Microsoft Corp"),
        }
        client.get_filing_document.side_effect = lambda _cik, accn, _doc, **_kw: xml_by_accn[accn]

        existing_dates = {
            "2026-03-31": "0001-26-000010",
            "2026-06-30": "0001-26-000021",
        }
        new_filings = fetch_new_etf_nport_filings(
            client, etf_cfg, existing_dates, max_age_days=0.5
        )
        self.assertEqual([(f.as_of, f.accession) for f in new_filings], [
            ("2026-03-31", "0001-26-000015"),
            ("2026-09-30", "0001-26-000030"),
        ])
        # Only the 2 new/amended XMLs were requested; stored/superseded XMLs were skipped
        fetched_accns = [call.args[1] for call in client.get_filing_document.call_args_list]
        self.assertEqual(fetched_accns, ["0001-26-000015", "0001-26-000030"])


class TestIncrementalUpsertAndSafetyGates(unittest.TestCase):
    def test_upsert_preserves_legacy_history_and_rebuilds_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            conn = init_db(db_path)
            try:
                # Seed an ETF with a legacy 2018-06-30 filing and SIC metadata on AAPL
                conn.execute(
                    "INSERT INTO etfs (etf_id, name, issuer, cik, structure, active) VALUES ('TST', 'Test', 'Issuer', '0000000001', 'open_end', 1)"
                )
                conn.execute(
                    """
                    INSERT INTO securities (security_id, ticker, name, cusip, cik, sic, sic_description, sector_group, first_seen, last_seen)
                    VALUES ('AAPL', 'AAPL', 'Apple Inc', '037833100', '0000320193', '3571', 'ELECTRONIC COMPUTERS', 'Technology', '2018-06-30', '2018-06-30')
                    """
                )
                conn.execute(
                    """
                    INSERT INTO securities (security_id, ticker, name, cusip, cik, first_seen, last_seen)
                    VALUES ('OLD', 'OLD', 'Old Corp', '111111111', '0000000002', '2018-06-30', '2018-06-30')
                    """
                )
                conn.execute(
                    "INSERT INTO etf_dates (etf_id, as_of, holdings_count, total_value_usd, source, accession) VALUES ('TST', '2018-06-30', 2, 1000.0, 'N-CSR', 'legacy-2018')"
                )
                conn.executemany(
                    """
                    INSERT INTO etf_holdings (etf_id, as_of, security_id, ticker, reported_name, shares, value_usd, weight, rank, prev_weight, is_new)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0)
                    """,
                    [
                        ("TST", "2018-06-30", "AAPL", "AAPL", "Apple Inc", 10, 600.0, 0.6, 1),
                        ("TST", "2018-06-30", "OLD", "OLD", "Old Corp", 10, 400.0, 0.4, 2),
                    ],
                )
                conn.commit()

                # Append a new 2026-09-30 N-PORT-P filing holding AAPL + MSFT (OLD removed)
                etf_cfg = {
                    "etf_id": "TST",
                    "name": "Test",
                    "issuer": "Issuer",
                    "cik": "0000000001",
                    "series_id": None,
                    "structure": "open_end",
                    "active": True,
                }
                new_filing = FilingHoldings(
                    etf_id="TST",
                    as_of="2026-09-30",
                    source="NPORT-P",
                    accession="0001-26-999999",
                    filed="2026-11-25",
                    total_value_usd=2000.0,
                    holdings=[
                        RawHolding("Apple Inc", "037833100", None, 10.0, 1100.0, 55.0),
                        RawHolding("Microsoft Corp", "594918104", None, 10.0, 900.0, 45.0),
                    ],
                )
                resolved = {
                    "2026-09-30": [
                        ResolvedHolding("AAPL", "AAPL", "Apple Inc", 10.0, 1100.0, 0.55, 55.0, 1, "037833100", None, "0000320193", "openfigi"),
                        ResolvedHolding("MSFT", "MSFT", "Microsoft Corp", 10.0, 900.0, 0.45, 45.0, 2, "594918104", None, "0000789019", "openfigi"),
                    ]
                }
                catalog = {
                    "AAPL": {
                        "security_id": "AAPL", "ticker": "AAPL", "name": "Apple Inc",
                        "cusip": "037833100", "isin": None, "cik": "0000320193",
                        "ticker_source": "openfigi", "first_seen": "2026-09-30", "last_seen": "2026-09-30",
                    },
                    "MSFT": {
                        "security_id": "MSFT", "ticker": "MSFT", "name": "Microsoft Corp",
                        "cusip": "594918104", "isin": None, "cik": "0000789019",
                        "ticker_source": "openfigi", "first_seen": "2026-09-30", "last_seen": "2026-09-30",
                    },
                }

                diffs = upsert_etf_holdings_incremental(conn, etf_cfg, [new_filing], resolved, catalog)
                self.assertEqual(len(diffs), 1)
                self.assertEqual(diffs[0].action, "added")
                self.assertEqual(diffs[0].added_tickers, ["MSFT"])
                self.assertEqual(diffs[0].removed_tickers, ["OLD"])

                # Legacy 2018-06-30 date and holdings are preserved
                dates = [r["as_of"] for r in conn.execute("SELECT as_of FROM etf_dates WHERE etf_id = 'TST' ORDER BY as_of").fetchall()]
                self.assertEqual(dates, ["2018-06-30", "2026-09-30"])

                # AAPL preserves SIC metadata and merged first_seen/last_seen
                aapl = conn.execute("SELECT sic, sector_group, first_seen, last_seen FROM securities WHERE security_id = 'AAPL'").fetchone()
                self.assertEqual(aapl["sic"], "3571")
                self.assertEqual(aapl["sector_group"], "Technology")
                self.assertEqual(aapl["first_seen"], "2018-06-30")
                self.assertEqual(aapl["last_seen"], "2026-09-30")

                # prev_weight and is_new are accurate on the appended quarter
                rows_new = {
                    r["security_id"]: (r["prev_weight"], r["is_new"])
                    for r in conn.execute(
                        "SELECT security_id, prev_weight, is_new FROM etf_holdings WHERE etf_id = 'TST' AND as_of = '2026-09-30'"
                    ).fetchall()
                }
                self.assertAlmostEqual(rows_new["AAPL"][0], 0.6)
                self.assertEqual(rows_new["AAPL"][1], 0)
                self.assertIsNone(rows_new["MSFT"][0])
                self.assertEqual(rows_new["MSFT"][1], 1)
            finally:
                conn.close()

    def test_no_changes_short_circuits_export_and_deploy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            log_path = Path(tmp) / "refresh.log"
            init_db(db_path).close()

            with (
                patch("src.refresh.load_etf_configs", return_value=[{"etf_id": "TST", "cik": "0000000001"}]),
                patch("src.refresh.fetch_new_etf_nport_filings", return_value=[]),
                patch("src.refresh.update_fundamentals_incremental", return_value=(10, 0, 0)),
                patch("src.refresh.update_sic_incremental", return_value=0),
                patch("src.refresh.export_site") as mock_export,
                patch("src.refresh.run_check_main") as mock_check,
                patch("src.refresh.run_deploy") as mock_deploy,
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                rc, summary = run_incremental_refresh(
                    db_path=db_path,
                    client=MagicMock(spec=EdgarClient),
                    deploy=True,
                    log_path=log_path,
                )

            self.assertEqual(rc, 0)
            self.assertFalse(summary.has_changes)
            self.assertFalse(summary.exported)
            self.assertFalse(summary.deployed)
            mock_export.assert_not_called()
            mock_check.assert_not_called()
            mock_deploy.assert_not_called()
            self.assertTrue(log_path.is_file())
            self.assertIn("NO CHANGES", log_path.read_text(encoding="utf-8"))

    def test_unresolved_ticker_aborts_before_db_write_and_deploy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            log_path = Path(tmp) / "refresh.log"
            init_db(db_path).close()

            fake_filing = FilingHoldings(
                etf_id="TST",
                as_of="2026-09-30",
                source="NPORT-P",
                accession="0001-26-888888",
                filed="2026-11-25",
                total_value_usd=1000.0,
                holdings=[RawHolding("Mystery NewCo", "999999999", None, 10.0, 1000.0, 100.0)],
            )
            unresolved_row = ResolvedHolding(
                security_id="CUSIP:999999999",
                ticker=None,
                reported_name="Mystery NewCo",
                shares=10.0,
                value_usd=1000.0,
                weight=1.0,
                pct_net_assets=100.0,
                rank=1,
                cusip="999999999",
                isin=None,
                cik=None,
                ticker_source="unresolved",
            )

            with (
                patch(
                    "src.refresh.load_etf_configs",
                    return_value=[{"etf_id": "TST", "name": "Test", "issuer": "I", "cik": "0000000001"}],
                ),
                patch("src.refresh.fetch_new_etf_nport_filings", return_value=[fake_filing]),
                patch(
                    "src.refresh.resolve_all_filings",
                    return_value=({"2026-09-30": [unresolved_row]}, {}),
                ),
                patch("src.refresh.export_site") as mock_export,
                patch("src.refresh.run_deploy") as mock_deploy,
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                rc, summary = run_incremental_refresh(
                    db_path=db_path,
                    client=MagicMock(spec=EdgarClient),
                    deploy=True,
                    log_path=log_path,
                )

            self.assertEqual(rc, 2)
            self.assertEqual(len(summary.unresolved_items), 1)
            self.assertEqual(summary.unresolved_items[0]["cusip"], "999999999")
            mock_export.assert_not_called()
            mock_deploy.assert_not_called()

            # Verify DB was NOT mutated
            conn = init_db(db_path)
            try:
                cnt = conn.execute("SELECT COUNT(*) FROM etf_dates").fetchone()[0]
                self.assertEqual(cnt, 0)
            finally:
                conn.close()

            report = format_summary_report(summary)
            self.assertIn("UNRESOLVED TICKERS", report)
            self.assertIn("Mystery NewCo", report)

    def test_failed_check_blocks_deploy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            log_path = Path(tmp) / "refresh.log"
            init_db(db_path).close()

            with (
                patch("src.refresh.load_etf_configs", return_value=[]),
                patch("src.refresh.update_fundamentals_incremental", return_value=(1, 1, 0)),
                patch("src.refresh.update_sic_incremental", return_value=0),
                patch("src.refresh.rebuild_quarter_prices", return_value=0),
                patch("src.refresh.rebuild_stock_metrics", return_value=0),
                patch(
                    "src.refresh.export_site",
                    return_value={"etfs": 1, "holdings_files": 1, "stock_files": 1},
                ),
                patch("src.refresh.run_check_main", return_value=1),
                patch("src.refresh.run_deploy") as mock_deploy,
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                rc, summary = run_incremental_refresh(
                    db_path=db_path,
                    client=MagicMock(spec=EdgarClient),
                    deploy=True,
                    log_path=log_path,
                )

            self.assertEqual(rc, 1)
            self.assertFalse(summary.check_passed)
            self.assertFalse(summary.deployed)
            mock_deploy.assert_not_called()


if __name__ == "__main__":
    unittest.main()
