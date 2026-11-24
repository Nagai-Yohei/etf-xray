"""Unit tests for official SEC SIC normalization and sector classification."""

from __future__ import annotations

import unittest

from src.sectors import classify_sector_group, normalize_sic


class TestSectorClassification(unittest.TestCase):
    def test_normalize_sic(self) -> None:
        self.assertEqual(normalize_sic("6798"), "6798")
        self.assertEqual(normalize_sic(1311), "1311")
        self.assertEqual(normalize_sic("602"), "0602")
        self.assertEqual(normalize_sic(" 4911 "), "4911")
        self.assertIsNone(normalize_sic(None))
        self.assertIsNone(normalize_sic(""))
        self.assertIsNone(normalize_sic("12345"))
        self.assertIsNone(normalize_sic("N/A"))

    def test_reit_classification(self) -> None:
        self.assertEqual(classify_sector_group("6798", "REAL ESTATE INVESTMENT TRUSTS"), "reit")
        self.assertEqual(classify_sector_group("6500", "REAL ESTATE INVESTMENT TRUST"), "reit")

    def test_bank_and_insurer_vs_broker_distinction(self) -> None:
        self.assertEqual(classify_sector_group("6021", "NATIONAL COMMERCIAL BANKS"), "bank")
        self.assertEqual(classify_sector_group("6141", "PERSONAL CREDIT INSTITUTIONS"), "bank")
        self.assertEqual(classify_sector_group("6331", "FIRE, MARINE & CASUALTY INSURANCE"), "insurance")
        # 6411 insurance agents/brokers are fee-based corporates, not balance-sheet underwriters
        self.assertIsNone(classify_sector_group("6411", "INSURANCE AGENTS, BROKERS & SERVICE"))

    def test_asset_mgmt_utility_energy_telecom_and_standard(self) -> None:
        self.assertEqual(classify_sector_group("6282", "INVESTMENT ADVICE"), "asset_mgmt")
        self.assertEqual(classify_sector_group("6211", "SECURITY BROKERS"), "asset_mgmt")
        self.assertEqual(classify_sector_group("4911", "ELECTRIC SERVICES"), "utility")
        # Midstream pipelines (4922) and waste services (4953) are excluded from regulated utilities
        self.assertIsNone(classify_sector_group("4922", "NATURAL GAS TRANSMISSION"))
        self.assertIsNone(classify_sector_group("4953", "REFUSE SYSTEMS"))
        self.assertEqual(classify_sector_group("1311", "CRUDE PETROLEUM & NATURAL GAS"), "energy_upstream")
        self.assertEqual(classify_sector_group("2911", "PETROLEUM REFINING"), "energy_upstream")
        self.assertEqual(classify_sector_group("4813", "TELEPHONE COMMUNICATIONS"), "telecom")
        self.assertIsNone(classify_sector_group("7372", "PREPACKAGED SOFTWARE"))
        self.assertIsNone(classify_sector_group(None))


if __name__ == "__main__":
    unittest.main()
