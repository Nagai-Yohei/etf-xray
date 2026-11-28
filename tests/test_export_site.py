"""Unit and temporary-SQLite integration tests for static JSON export and dist validation."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import check as integrity_check
from src.db import init_db
from src.export_site import (
    _build_compare_overview,
    _strip_nulls,
    export_site,
    stock_shard_id,
)


class StripNullsTests(unittest.TestCase):
    def test_removes_none_dict_keys_while_preserving_falsy_values(self) -> None:
        raw = {
            "keep_zero": 0,
            "keep_float_zero": 0.0,
            "keep_false": False,
            "keep_empty_str": "",
            "keep_empty_list": [],
            "keep_empty_dict": {},
            "drop_none": None,
            "nested": {
                "a": 1,
                "b": None,
                "items": [{"x": None, "y": 2}, None],
            },
        }
        cleaned = _strip_nulls(raw)
        self.assertEqual(
            cleaned,
            {
                "keep_zero": 0,
                "keep_float_zero": 0.0,
                "keep_false": False,
                "keep_empty_str": "",
                "keep_empty_list": [],
                "keep_empty_dict": {},
                "nested": {
                    "a": 1,
                    "items": [{"y": 2}, None],
                },
            },
        )


class CompareOverviewTests(unittest.TestCase):
    def test_overlap_matrix_and_core_holdings(self) -> None:
        etf_ids = ["AAA", "BBB", "CCC"]
        latest_holdings = {
            "AAA": [
                {"security_id": "MSFT", "ticker": "MSFT", "name": "Microsoft", "weight": 0.6},
                {"security_id": "KO", "ticker": "KO", "name": "Coca-Cola", "weight": 0.4},
            ],
            "BBB": [
                {"security_id": "MSFT", "ticker": "MSFT", "name": "Microsoft", "weight": 0.25},
                {"security_id": "JNJ", "ticker": "JNJ", "name": "Johnson & Johnson", "weight": 0.75},
            ],
            "CCC": [
                {"security_id": "MSFT", "ticker": "MSFT", "name": "Microsoft", "weight": 0.5},
                {"security_id": "KO", "ticker": "KO", "name": "Coca-Cola", "weight": 0.3},
                {"security_id": "UNIQ", "ticker": "UNIQ", "name": "Unique Co", "weight": 0.2},
            ],
        }
        ov = _build_compare_overview(etf_ids, latest_holdings)
        self.assertEqual(ov["etf_ids"], etf_ids)

        mat = ov["matrix"]
        self.assertEqual(mat["AAA"]["AAA"], {"overlapWeight": 1.0, "commonCount": 2, "self": True})
        self.assertAlmostEqual(mat["AAA"]["BBB"]["overlapWeight"], 0.25)
        self.assertEqual(mat["AAA"]["BBB"]["commonCount"], 1)
        self.assertFalse(mat["AAA"]["BBB"]["self"])
        # Symmetry check
        self.assertEqual(mat["AAA"]["CCC"], mat["CCC"]["AAA"])
        self.assertAlmostEqual(mat["AAA"]["CCC"]["overlapWeight"], 0.5 + 0.3)
        self.assertEqual(mat["AAA"]["CCC"]["commonCount"], 2)

        # Core holdings should include securities held in >= 2 ETFs sorted by etf_count desc, avg_weight desc
        core = ov["core_holdings"]
        self.assertEqual([r["security_id"] for r in core], ["MSFT", "KO"])
        self.assertEqual(core[0]["etf_count"], 3)
        self.assertAlmostEqual(core[0]["avg_weight"], (0.6 + 0.25 + 0.5) / 3, places=5)
        self.assertEqual(core[1]["etf_count"], 2)
        self.assertEqual(core[1]["weights"], {"AAA": 0.4, "CCC": 0.3})


class ExportSiteIntegrationTests(unittest.TestCase):
    def test_export_split_files_and_check_dist_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "market.db"
            dist_dir = root / "dist"

            conn = init_db(db_path)
            try:
                conn.executemany(
                    "INSERT INTO etfs (etf_id, name, issuer, cik, series_id, structure) VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        ("AAA", "Alpha ETF", "Issuer A", "0000000001", "S000000001", "open_end"),
                        ("BBB", "Beta ETF", "Issuer B", "0000000002", "S000000002", "open_end"),
                    ],
                )
                conn.executemany(
                    "INSERT INTO securities (security_id, ticker, name, cik, first_seen, last_seen) VALUES (?, ?, ?, NULL, ?, ?)",
                    [
                        ("MSFT", "MSFT", "Microsoft Corp", "2025-12-31", "2026-03-31"),
                        ("OLDCO", "OLDCO", "Old Company", "2025-12-31", "2025-12-31"),
                        ("NEWCO", "NEWCO", "New Company", "2026-03-31", "2026-03-31"),
                    ],
                )
                conn.executemany(
                    "INSERT INTO etf_dates (etf_id, as_of, holdings_count, total_value_usd, source, accession) VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        ("AAA", "2025-12-31", 2, 1000.0, "NPORT-P", "0001-25-000001"),
                        ("AAA", "2026-03-31", 2, 1200.0, "NPORT-P", "0001-26-000002"),
                        ("BBB", "2026-03-31", 1, 500.0, "NPORT-P", "0002-26-000001"),
                    ],
                )
                conn.executemany(
                    """
                    INSERT INTO etf_holdings (
                        etf_id, as_of, security_id, ticker, reported_name,
                        shares, value_usd, weight, pct_net_assets, rank, prev_weight, is_new
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        ("AAA", "2025-12-31", "MSFT", "MSFT", "Microsoft Corp", 10.0, 600.0, 0.6, None, 1, None, 0),
                        ("AAA", "2025-12-31", "OLDCO", "OLDCO", "Old Company", 20.0, 400.0, 0.4, None, 2, None, 0),
                        ("AAA", "2026-03-31", "MSFT", "MSFT", "Microsoft Corp", 10.0, 720.0, 0.6, None, 1, 0.6, 0),
                        ("AAA", "2026-03-31", "NEWCO", "NEWCO", "New Company", 12.0, 480.0, 0.4, None, 2, None, 1),
                        ("BBB", "2026-03-31", "MSFT", "MSFT", "Microsoft Corp", 7.0, 500.0, 1.0, None, 1, None, 0),
                    ],
                )
                conn.executemany(
                    "INSERT INTO etf_changes (etf_id, as_of, kind, security_id, ticker, name, weight) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [
                        ("AAA", "2026-03-31", "add", "NEWCO", "NEWCO", "New Company", 0.4),
                        ("AAA", "2026-03-31", "remove", "OLDCO", "OLDCO", "Old Company", 0.4),
                    ],
                )
                conn.commit()
            finally:
                conn.close()

            stats = export_site(db_path=db_path, dist_dir=dist_dir)
            self.assertEqual(stats["etfs"], 2)
            self.assertEqual(stats["holdings_files"], 3)
            self.assertEqual(stats["stock_count"], 3)

            # summary.json should omit heavyweight changes and tenure_map while keeping counts
            aaa_sum = json.loads((dist_dir / "data/etf/AAA/summary.json").read_text(encoding="utf-8"))
            self.assertNotIn("changes", aaa_sum)
            self.assertNotIn("tenure_map", aaa_sum)
            self.assertEqual(aaa_sum["tenure_current_count"], 2)
            self.assertEqual(aaa_sum["tenure_total_count"], 3)

            # tenure.json should contain the split tenure map
            aaa_tenure = json.loads((dist_dir / "data/etf/AAA/tenure.json").read_text(encoding="utf-8"))
            self.assertEqual(aaa_tenure["current_count"], 2)
            self.assertEqual(aaa_tenure["total_count"], 3)
            self.assertEqual(len(aaa_tenure["tenure_map"]), 3)

            # holdings/<as_of>.json should carry per-quarter added/removed and omit null fields
            h_latest = json.loads(
                (dist_dir / "data/etf/AAA/holdings/2026-03-31.json").read_text(encoding="utf-8")
            )
            self.assertEqual([x["security_id"] for x in h_latest["added"]], ["NEWCO"])
            self.assertEqual([x["security_id"] for x in h_latest["removed"]], ["OLDCO"])
            self.assertNotIn("pct_net_assets", h_latest["holdings"][0])

            # compare/overview.json should contain precomputed matrix and core_holdings
            compare_ov = json.loads((dist_dir / "data/compare/overview.json").read_text(encoding="utf-8"))
            self.assertEqual(compare_ov["etf_ids"], ["AAA", "BBB"])
            self.assertAlmostEqual(compare_ov["matrix"]["AAA"]["BBB"]["overlapWeight"], 0.6)
            self.assertEqual([r["security_id"] for r in compare_ov["core_holdings"]], ["MSFT"])

            # Split stock indexes should omit None fields
            idx_active = json.loads((dist_dir / "data/stocks/index-active.json").read_text(encoding="utf-8"))
            idx_former = json.loads((dist_dir / "data/stocks/index-former.json").read_text(encoding="utf-8"))
            self.assertEqual(sorted(idx_active.keys()), ["MSFT", "NEWCO"])
            self.assertEqual(sorted(idx_former.keys()), ["OLDCO"])
            self.assertNotIn("cik", idx_former["OLDCO"])
            self.assertNotIn("pe_ratio", idx_former["OLDCO"])

            # Shard lookup sanity
            msft_shard = json.loads(
                (dist_dir / f"data/stocks/shards/{stock_shard_id('MSFT')}.json").read_text(encoding="utf-8")
            )
            self.assertIn("MSFT", msft_shard)

            # check_dist_json should pass with zero errors
            errors: list[str] = []
            with mock.patch.object(integrity_check, "DIST_DIR", dist_dir):
                status = integrity_check.check_dist_json(errors)
            self.assertEqual(errors, [])
            self.assertTrue(status.startswith("OK ("))


if __name__ == "__main__":
    unittest.main()
