"""Unit tests for entity resolution helpers (ticker normalization, name keys, manual overrides)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.identify import (
    is_non_common_ticker,
    load_manual_tickers,
    normalize_company_name,
    normalize_ticker,
)


class TestIdentifyHelpers(unittest.TestCase):
    def test_normalize_ticker_unifies_slash_and_hyphen(self) -> None:
        self.assertEqual(normalize_ticker("bf/b"), "BF.B")
        self.assertEqual(normalize_ticker("BRK-B"), "BRK.B")
        self.assertEqual(normalize_ticker(" CWEN/A "), "CWEN.A")
        self.assertIsNone(normalize_ticker(""))
        self.assertIsNone(normalize_ticker(None))

    def test_is_non_common_ticker_filters_warrants_preferreds_and_rights(self) -> None:
        for non_common in ("OXY.WS", "XYZ.WT", "AHL.PD", "ABC.PR", "TMUSR"):
            self.assertTrue(is_non_common_ticker(non_common), non_common)
        for common in ("AAPL", "BRK.B", "BF.B", "CWEN.A", "GOOGL", "OXY"):
            self.assertFalse(is_non_common_ticker(common), common)

    def test_normalize_company_name_preserves_dual_class_by_default(self) -> None:
        cls_a = normalize_company_name("Alphabet Inc. Class A")
        cls_c = normalize_company_name("Alphabet Inc. Class C")
        self.assertNotEqual(cls_a, cls_c)
        self.assertTrue(cls_a.endswith("class a"))
        self.assertTrue(cls_c.endswith("class c"))
        # Strip "The" prefix/suffix and apostrophes consistently
        self.assertEqual(
            normalize_company_name("The Coca-Cola Company"),
            normalize_company_name("Coca-Cola Co./The"),
        )
        self.assertEqual(
            normalize_company_name("McDonald's Corp."),
            normalize_company_name("McDonalds Corporation"),
        )

    def test_load_manual_tickers_separates_cusip_and_name_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "manual.csv"
            csv_path.write_text(
                "name,cusip,ticker,note\n"
                "Clearway Energy Inc,18539C105,CWEN/A,Class A CUSIP\n"
                "Wal-Mart Stores Inc.,,WMT,Legacy name\n",
                encoding="utf-8",
            )
            by_cusip, by_exact, by_norm = load_manual_tickers(csv_path)
            self.assertEqual(by_cusip, {"18539C105": "CWEN.A"})
            self.assertNotIn("clearway energy inc", by_exact)
            self.assertEqual(by_exact["wal-mart stores inc."], "WMT")
            self.assertEqual(by_norm[normalize_company_name("Wal-Mart Stores Inc.")], "WMT")


if __name__ == "__main__":
    unittest.main()
