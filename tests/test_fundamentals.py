"""Unit and regression tests for SEC XBRL fundamentals extraction and entity guards."""

from __future__ import annotations

import unittest
from typing import Any

from src.fundamentals import (
    extract_annual_financials,
    extract_quarterly_financials,
    extract_ttm_financials,
    merge_company_facts,
)
from src.identify import is_non_common_ticker


def _make_facts(
    us_gaap: dict[str, dict[str, list[dict[str, Any]]]],
    dei: dict[str, dict[str, list[dict[str, Any]]]] | None = None,
) -> dict[str, Any]:
    """Build a minimal SEC companyfacts JSON structure."""
    facts: dict[str, Any] = {
        "us-gaap": {tag: {"units": units} for tag, units in us_gaap.items()}
    }
    if dei:
        facts["dei"] = {tag: {"units": units} for tag, units in dei.items()}
    return {"facts": facts}


def _dur(
    start: str,
    end: str,
    val: float,
    filed: str,
    form: str = "10-K",
    accn: str | None = None,
) -> dict[str, Any]:
    return {
        "start": start,
        "end": end,
        "val": val,
        "filed": filed,
        "form": form,
        "accn": accn or f"0000000000-{filed[:4]}-{filed[5:7]}{filed[8:10]}",
    }


def _inst(
    end: str,
    val: float,
    filed: str,
    form: str = "10-K",
    accn: str | None = None,
) -> dict[str, Any]:
    return {
        "end": end,
        "val": val,
        "filed": filed,
        "form": form,
        "accn": accn or f"0000000000-{filed[:4]}-{filed[5:7]}{filed[8:10]}",
    }


class TestStockSplits(unittest.TestCase):
    def test_standard_2_for_1_split_from_10k_overlap(self) -> None:
        cf = _make_facts(
            {
                "Revenues": {
                    "USD": [
                        _dur("2022-01-01", "2022-12-31", 1_000_000_000, "2023-02-20"),
                        _dur("2023-01-01", "2023-12-31", 1_100_000_000, "2024-02-20"),
                        _dur("2024-01-01", "2024-12-31", 1_200_000_000, "2025-02-20"),
                    ]
                },
                "NetIncomeLoss": {
                    "USD": [
                        _dur("2022-01-01", "2022-12-31", 200_000_000, "2023-02-20"),
                        _dur("2023-01-01", "2023-12-31", 220_000_000, "2024-02-20"),
                        _dur("2024-01-01", "2024-12-31", 240_000_000, "2025-02-20"),
                    ]
                },
                "WeightedAverageNumberOfDilutedSharesOutstanding": {
                    "shares": [
                        _dur("2022-01-01", "2022-12-31", 100_000_000, "2023-02-20"),
                        _dur("2023-01-01", "2023-12-31", 100_000_000, "2024-02-20"),
                        # 2024 10-K restates 2023 on post-2:1 split basis
                        _dur("2023-01-01", "2023-12-31", 200_000_000, "2025-02-20"),
                        _dur("2024-01-01", "2024-12-31", 200_000_000, "2025-02-20"),
                    ]
                },
                "EarningsPerShareDiluted": {
                    "USD/shares": [
                        _dur("2022-01-01", "2022-12-31", 2.00, "2023-02-20"),
                        _dur("2023-01-01", "2023-12-31", 2.20, "2024-02-20"),
                        _dur("2023-01-01", "2023-12-31", 1.10, "2025-02-20"),
                        _dur("2024-01-01", "2024-12-31", 1.20, "2025-02-20"),
                    ]
                },
            }
        )
        rows, splits = extract_annual_financials(cf)
        self.assertEqual(splits, [("2025-02-20", 2.0)])
        by_end = {r["fy_end"]: r for r in rows}
        self.assertAlmostEqual(by_end["2022-12-31"]["eps_diluted"], 1.00, places=4)
        self.assertAlmostEqual(by_end["2022-12-31"]["shares_diluted"], 200_000_000.0)
        self.assertAlmostEqual(by_end["2023-12-31"]["eps_diluted"], 1.10, places=4)
        self.assertAlmostEqual(by_end["2024-12-31"]["eps_diluted"], 1.20, places=4)

    def test_large_reverse_split_with_modest_merger_ni_shift(self) -> None:
        """SIRI-style 1:10 reverse split + split-off merger shifting comparative NI ~11%."""
        cf = _make_facts(
            {
                "Revenues": {
                    "USD": [
                        _dur("2021-01-01", "2021-12-31", 8_700_000_000, "2024-02-01"),
                        _dur("2022-01-01", "2022-12-31", 9_000_000_000, "2025-01-30"),
                        _dur("2023-01-01", "2023-12-31", 8_950_000_000, "2025-01-30"),
                        _dur("2024-01-01", "2024-12-31", 8_700_000_000, "2025-01-30"),
                    ]
                },
                "NetIncomeLoss": {
                    "USD": [
                        _dur("2021-01-01", "2021-12-31", 1_310_000_000, "2024-02-01"),
                        _dur("2022-01-01", "2022-12-31", 1_090_000_000, "2025-01-30"),
                        _dur("2023-01-01", "2023-12-31", 1_000_000_000, "2025-01-30"),
                        _dur("2024-01-01", "2024-12-31", -2_080_000_000, "2025-01-30"),
                    ]
                },
                "WeightedAverageNumberOfDilutedSharesOutstanding": {
                    "shares": [
                        _dur("2021-01-01", "2021-12-31", 4_143_000_000, "2024-02-01"),
                        _dur("2022-01-01", "2022-12-31", 3_990_000_000, "2024-02-01"),
                        _dur("2022-01-01", "2022-12-31", 368_000_000, "2025-01-30"),
                        _dur("2023-01-01", "2023-12-31", 362_000_000, "2025-01-30"),
                        _dur("2024-01-01", "2024-12-31", 338_000_000, "2025-01-30"),
                    ]
                },
                "EarningsPerShareDiluted": {
                    "USD/shares": [
                        _dur("2021-01-01", "2021-12-31", 0.32, "2024-02-01"),
                        _dur("2022-01-01", "2022-12-31", 0.31, "2024-02-01"),
                        _dur("2022-01-01", "2022-12-31", 2.96, "2025-01-30"),
                        _dur("2023-01-01", "2023-12-31", 2.77, "2025-01-30"),
                        _dur("2024-01-01", "2024-12-31", -6.14, "2025-01-30"),
                    ]
                },
                "CommonStockDividendsPerShareCashPaid": {
                    "USD/shares": [
                        _dur("2021-01-01", "2021-12-31", 0.0659, "2024-02-01"),
                    ]
                },
            }
        )
        rows, splits = extract_annual_financials(cf)
        self.assertEqual(len(splits), 1)
        self.assertEqual(splits[0][0], "2025-01-30")
        self.assertAlmostEqual(splits[0][1], 0.0922, places=3)
        by_end = {r["fy_end"]: r for r in rows}
        # 2021 pre-reverse-split DPS and shares adjusted onto post-merger basis
        self.assertAlmostEqual(by_end["2021-12-31"]["dps_paid"], 0.0659 / splits[0][1], places=3)
        self.assertAlmostEqual(by_end["2021-12-31"]["shares_diluted"], 4_143_000_000 * splits[0][1], delta=1_000_000)

    def test_multi_split_quarterly_eps_does_not_collide_with_earlier_split(self) -> None:
        """APH/MNST/SFBS-style: 2:1 split in 2023 AND 2:1 split in Q3 2026 must adjust Q1/Q2 2026."""
        cf = _make_facts(
            {
                "Revenues": {
                    "USD": [
                        _dur("2022-01-01", "2022-12-31", 6_000_000_000, "2023-02-20"),
                        _dur("2023-01-01", "2023-12-31", 7_000_000_000, "2024-02-20"),
                        _dur("2024-01-01", "2024-12-31", 8_000_000_000, "2025-02-20"),
                        _dur("2025-01-01", "2025-12-31", 9_000_000_000, "2026-02-20"),
                        # Standalone quarters
                        _dur("2025-04-01", "2025-06-30", 2_200_000_000, "2025-08-05", "10-Q"),
                        _dur("2025-07-01", "2025-09-30", 2_300_000_000, "2025-11-05", "10-Q"),
                        _dur("2026-01-01", "2026-03-31", 2_400_000_000, "2026-05-05", "10-Q"),
                        _dur("2026-04-01", "2026-06-30", 2_500_000_000, "2026-08-05", "10-Q"),
                        _dur("2026-07-01", "2026-09-30", 2_600_000_000, "2026-11-05", "10-Q"),
                    ]
                },
                "NetIncomeLoss": {
                    "USD": [
                        _dur("2022-01-01", "2022-12-31", 1_000_000_000, "2023-02-20"),
                        _dur("2023-01-01", "2023-12-31", 1_200_000_000, "2024-02-20"),
                        _dur("2024-01-01", "2024-12-31", 1_400_000_000, "2025-02-20"),
                        _dur("2025-01-01", "2025-12-31", 1_600_000_000, "2026-02-20"),
                        _dur("2025-04-01", "2025-06-30", 400_000_000, "2025-08-05", "10-Q"),
                        _dur("2025-07-01", "2025-09-30", 420_000_000, "2025-11-05", "10-Q"),
                        _dur("2026-01-01", "2026-03-31", 440_000_000, "2026-05-05", "10-Q"),
                        _dur("2026-04-01", "2026-06-30", 460_000_000, "2026-08-05", "10-Q"),
                        _dur("2026-07-01", "2026-09-30", 480_000_000, "2026-11-05", "10-Q"),
                    ]
                },
                "WeightedAverageNumberOfDilutedSharesOutstanding": {
                    "shares": [
                        # First 2:1 split in 2023 (500M -> 1000M)
                        _dur("2022-01-01", "2022-12-31", 500_000_000, "2023-02-20"),
                        _dur("2022-01-01", "2022-12-31", 1_000_000_000, "2024-02-20"),
                        _dur("2023-01-01", "2023-12-31", 1_000_000_000, "2024-02-20"),
                        _dur("2024-01-01", "2024-12-31", 1_000_000_000, "2025-02-20"),
                        _dur("2025-01-01", "2025-12-31", 1_000_000_000, "2026-02-20"),
                        # Second 2:1 split in Q3 2026 (1000M -> 2000M) visible in Q3 2026 comparative 10-Q
                        _dur("2025-07-01", "2025-09-30", 1_000_000_000, "2025-11-05", "10-Q"),
                        _dur("2025-07-01", "2025-09-30", 2_000_000_000, "2026-11-05", "10-Q"),
                        _dur("2026-07-01", "2026-09-30", 2_000_000_000, "2026-11-05", "10-Q"),
                    ]
                },
                "EarningsPerShareDiluted": {
                    "USD/shares": [
                        # Earlier 2023 10-Q comparative showing the first 2:1 split
                        _dur("2022-04-01", "2022-06-30", 0.50, "2022-08-05", "10-Q"),
                        _dur("2022-04-01", "2022-06-30", 0.25, "2023-08-05", "10-Q"),
                        # 10-Ks
                        _dur("2022-01-01", "2022-12-31", 2.00, "2023-02-20"),
                        _dur("2022-01-01", "2022-12-31", 1.00, "2024-02-20"),
                        _dur("2023-01-01", "2023-12-31", 1.20, "2024-02-20"),
                        _dur("2024-01-01", "2024-12-31", 1.40, "2025-02-20"),
                        _dur("2025-01-01", "2025-12-31", 1.60, "2026-02-20"),
                        # 2025-2026 10-Qs: Q1/Q2 2026 filed pre-split, Q3 2026 filed post-split
                        _dur("2025-04-01", "2025-06-30", 0.40, "2025-08-05", "10-Q"),
                        _dur("2025-07-01", "2025-09-30", 0.42, "2025-11-05", "10-Q"),
                        _dur("2025-07-01", "2025-09-30", 0.21, "2026-11-05", "10-Q"),
                        _dur("2026-01-01", "2026-03-31", 0.44, "2026-05-05", "10-Q"),
                        _dur("2026-04-01", "2026-06-30", 0.46, "2026-08-05", "10-Q"),
                        _dur("2026-07-01", "2026-09-30", 0.24, "2026-11-05", "10-Q"),
                    ]
                },
            }
        )
        annual_rows, splits = extract_annual_financials(cf)
        self.assertEqual(splits, [("2024-02-20", 2.0), ("2026-11-05", 2.0)])
        q_rows = extract_quarterly_financials(cf, annual_rows, splits)
        q_by_end = {q["period_end"]: q for q in q_rows}
        # All 2025-2026 quarters must be consistently on the post-Q3-2026 split basis
        self.assertAlmostEqual(q_by_end["2025-06-30"]["eps_diluted"], 0.20, places=4)
        self.assertAlmostEqual(q_by_end["2025-09-30"]["eps_diluted"], 0.21, places=4)
        self.assertAlmostEqual(q_by_end["2026-03-31"]["eps_diluted"], 0.22, places=4)
        self.assertAlmostEqual(q_by_end["2026-06-30"]["eps_diluted"], 0.23, places=4)
        self.assertAlmostEqual(q_by_end["2026-09-30"]["eps_diluted"], 0.24, places=4)

    def test_midyear_split_refines_quarterly_date_without_double_dividing(self) -> None:
        """CTAS-style: 4:1 split in Sep 2024 before Jul 2025 10-K must not double-divide post-split 10-Qs."""
        cf = _make_facts(
            {
                "Revenues": {
                    "USD": [
                        _dur("2023-06-01", "2024-05-31", 9_600_000_000, "2024-07-25"),
                        _dur("2024-06-01", "2025-05-31", 10_300_000_000, "2025-07-25"),
                        _dur("2024-06-01", "2024-08-31", 2_500_000_000, "2024-10-08", "10-Q"),
                        _dur("2024-09-01", "2024-11-30", 2_550_000_000, "2025-01-08", "10-Q"),
                        _dur("2024-12-01", "2025-02-28", 2_600_000_000, "2025-04-08", "10-Q"),
                    ]
                },
                "NetIncomeLoss": {
                    "USD": [
                        _dur("2023-06-01", "2024-05-31", 1_570_000_000, "2024-07-25"),
                        _dur("2024-06-01", "2025-05-31", 1_800_000_000, "2025-07-25"),
                        _dur("2024-06-01", "2024-08-31", 440_000_000, "2024-10-08", "10-Q"),
                        _dur("2024-09-01", "2024-11-30", 445_000_000, "2025-01-08", "10-Q"),
                        _dur("2024-12-01", "2025-02-28", 455_000_000, "2025-04-08", "10-Q"),
                    ]
                },
                "WeightedAverageNumberOfDilutedSharesOutstanding": {
                    "shares": [
                        _dur("2023-06-01", "2024-05-31", 103_000_000, "2024-07-25"),
                        _dur("2023-06-01", "2024-05-31", 412_000_000, "2025-07-25"),
                        _dur("2024-06-01", "2025-05-31", 410_000_000, "2025-07-25"),
                    ]
                },
                "EarningsPerShareDiluted": {
                    "USD/shares": [
                        _dur("2023-06-01", "2024-05-31", 15.16, "2024-07-25"),
                        _dur("2023-06-01", "2024-05-31", 3.79, "2025-07-25"),
                        _dur("2024-06-01", "2025-05-31", 4.40, "2025-07-25"),
                        # Prior-year Q1 comparative reported pre-split in Oct 2023, post-split in Oct 2024
                        _dur("2023-06-01", "2023-08-31", 3.72, "2023-10-08", "10-Q"),
                        _dur("2023-06-01", "2023-08-31", 0.93, "2024-10-08", "10-Q"),
                        # Post-split FY25 10-Qs filed BEFORE Jul 2025 10-K
                        _dur("2024-06-01", "2024-08-31", 1.10, "2024-10-08", "10-Q"),
                        _dur("2024-09-01", "2024-11-30", 1.09, "2025-01-08", "10-Q"),
                        _dur("2024-12-01", "2025-02-28", 1.13, "2025-04-08", "10-Q"),
                    ]
                },
            }
        )
        annual_rows, splits = extract_annual_financials(cf)
        self.assertEqual(splits, [("2025-07-25", 4.0)])
        q_rows = extract_quarterly_financials(cf, annual_rows, splits)
        q_by_end = {q["period_end"]: q for q in q_rows}
        # Post-split quarters filed before Jul 2025 10-K must stay 1.10 / 1.09 / 1.13 (NOT divided by 4)
        self.assertAlmostEqual(q_by_end["2024-08-31"]["eps_diluted"], 1.10, places=4)
        self.assertAlmostEqual(q_by_end["2024-11-30"]["eps_diluted"], 1.09, places=4)
        self.assertAlmostEqual(q_by_end["2025-02-28"]["eps_diluted"], 1.13, places=4)

    def test_split_already_reflected_in_latest_10k_is_not_double_applied(self) -> None:
        """AMCR-style: 1:5 reverse split already reflected in latest 10-K must not trigger post-10K split."""
        cf = _make_facts(
            {
                "Revenues": {"USD": [_dur("2025-07-01", "2026-06-30", 14_000_000_000, "2026-08-20")]},
                "NetIncomeLoss": {"USD": [_dur("2025-07-01", "2026-06-30", 900_000_000, "2026-08-20")]},
                "WeightedAverageNumberOfDilutedSharesOutstanding": {
                    "shares": [
                        # Latest 10-K already on post-1:5 reverse-split basis (~300M shares)
                        _dur("2025-07-01", "2026-06-30", 300_000_000, "2026-08-20"),
                        # Prior Q1 10-Q (pre-split 1500M) vs new Q1 10-Q (post-split 300M)
                        _dur("2025-07-01", "2025-09-30", 1_500_000_000, "2025-11-05", "10-Q"),
                        _dur("2025-07-01", "2025-09-30", 300_000_000, "2026-11-05", "10-Q"),
                    ]
                },
                "EarningsPerShareDiluted": {
                    "USD/shares": [
                        _dur("2025-07-01", "2026-06-30", 3.00, "2026-08-20"),
                        _dur("2025-07-01", "2025-09-30", 0.15, "2025-11-05", "10-Q"),
                        _dur("2025-07-01", "2025-09-30", 0.75, "2026-11-05", "10-Q"),
                    ]
                },
            }
        )
        rows, splits = extract_annual_financials(cf)
        self.assertEqual(splits, [])
        self.assertAlmostEqual(rows[-1]["eps_diluted"], 3.00, places=4)


class TestRevenueExtraction(unittest.TestCase):
    def test_zero_revenue_placeholder_overridden_by_asc606(self) -> None:
        """FLS-style: Revenues=0.0 placeholder must fall through to positive ASC 606 revenue."""
        cf = _make_facts(
            {
                "Revenues": {
                    "USD": [
                        _dur("2025-01-01", "2025-12-31", 0.0, "2026-02-17"),
                        _dur("2025-01-01", "2025-09-30", 0.0, "2025-10-30", "10-Q"),
                        _dur("2026-01-01", "2026-09-30", 0.0, "2026-10-30", "10-Q"),
                        _dur("2026-07-01", "2026-09-30", 0.0, "2026-10-30", "10-Q"),
                    ]
                },
                "RevenueFromContractWithCustomerExcludingAssessedTax": {
                    "USD": [
                        _dur("2025-01-01", "2025-12-31", 4_729_000_000, "2026-02-17"),
                        _dur("2025-01-01", "2025-09-30", 3_500_000_000, "2025-10-30", "10-Q"),
                        _dur("2026-01-01", "2026-09-30", 3_600_000_000, "2026-10-30", "10-Q"),
                        _dur("2026-07-01", "2026-09-30", 1_238_000_000, "2026-10-30", "10-Q"),
                    ]
                },
                "NetIncomeLoss": {
                    "USD": [
                        _dur("2025-01-01", "2025-12-31", 346_000_000, "2026-02-17"),
                        _dur("2025-01-01", "2025-09-30", 250_000_000, "2025-10-30", "10-Q"),
                        _dur("2026-01-01", "2026-09-30", 280_000_000, "2026-10-30", "10-Q"),
                        _dur("2026-07-01", "2026-09-30", 103_000_000, "2026-10-30", "10-Q"),
                    ]
                },
            }
        )
        annual_rows, splits = extract_annual_financials(cf)
        self.assertEqual(annual_rows[-1]["revenue"], 4_729_000_000.0)
        ttm = extract_ttm_financials(cf, annual_rows)
        self.assertIsNotNone(ttm)
        self.assertEqual(ttm["revenue_ttm"], 4_829_000_000.0)
        q_rows = extract_quarterly_financials(cf, annual_rows, splits)
        q_by_end = {q["period_end"]: q for q in q_rows}
        self.assertEqual(q_by_end["2026-09-30"]["revenue"], 1_238_000_000.0)
        self.assertEqual(q_by_end["2025-12-31"]["revenue"], 1_229_000_000.0)

    def test_partial_revenues_overridden_by_asc606_in_annual_and_quarterly(self) -> None:
        """OWL-style: partial Revenues tag consistently smaller than ASC 606 total."""
        cf = _make_facts(
            {
                "Revenues": {
                    "USD": [
                        _dur("2024-01-01", "2024-12-31", 450_000_000, "2025-02-20"),
                        _dur("2025-01-01", "2025-12-31", 568_000_000, "2026-02-19"),
                        _dur("2025-01-01", "2025-09-30", 421_000_000, "2025-11-03", "10-Q"),
                        _dur("2026-01-01", "2026-03-31", 138_000_000, "2026-05-01", "10-Q"),
                        _dur("2026-04-01", "2026-06-30", 137_000_000, "2026-07-31", "10-Q"),
                    ]
                },
                "RevenueFromContractWithCustomerExcludingAssessedTax": {
                    "USD": [
                        _dur("2024-01-01", "2024-12-31", 2_295_000_000, "2025-02-20"),
                        _dur("2025-01-01", "2025-12-31", 2_870_000_000, "2026-02-19"),
                        _dur("2025-01-01", "2025-09-30", 2_115_000_000, "2025-11-03", "10-Q"),
                        _dur("2026-01-01", "2026-03-31", 754_000_000, "2026-05-01", "10-Q"),
                        _dur("2026-04-01", "2026-06-30", 753_000_000, "2026-07-31", "10-Q"),
                        _dur("2026-07-01", "2026-09-30", 806_000_000, "2026-10-30", "10-Q"),
                    ]
                },
                "NetIncomeLoss": {
                    "USD": [
                        _dur("2024-01-01", "2024-12-31", 110_000_000, "2025-02-20"),
                        _dur("2025-01-01", "2025-12-31", 79_000_000, "2026-02-19"),
                        _dur("2025-01-01", "2025-09-30", 31_000_000, "2025-11-03", "10-Q"),
                        _dur("2026-01-01", "2026-03-31", 15_000_000, "2026-05-01", "10-Q"),
                        _dur("2026-04-01", "2026-06-30", 11_000_000, "2026-07-31", "10-Q"),
                        _dur("2026-07-01", "2026-09-30", 20_000_000, "2026-10-30", "10-Q"),
                    ]
                },
            }
        )
        annual_rows, splits = extract_annual_financials(cf)
        self.assertEqual(annual_rows[-1]["revenue"], 2_870_000_000.0)
        q_rows = extract_quarterly_financials(cf, annual_rows, splits)
        q_by_end = {q["period_end"]: q for q in q_rows}
        self.assertEqual(q_by_end["2025-12-31"]["revenue"], 755_000_000.0)
        self.assertEqual(q_by_end["2026-03-31"]["revenue"], 754_000_000.0)
        self.assertEqual(q_by_end["2026-06-30"]["revenue"], 753_000_000.0)
        self.assertEqual(q_by_end["2026-09-30"]["revenue"], 806_000_000.0)

    def test_gross_profit_exceeding_partial_revenue_switches_tag(self) -> None:
        """AIT-style: segment Revenues=$24M < GrossProfit=$770M switches to SalesRevenueNet=$2.75B."""
        cf = _make_facts(
            {
                "Revenues": {"USD": [_dur("2015-07-01", "2016-06-30", 24_000_000, "2016-08-25")]},
                "SalesRevenueNet": {"USD": [_dur("2015-07-01", "2016-06-30", 2_750_000_000, "2016-08-25")]},
                "GrossProfit": {"USD": [_dur("2015-07-01", "2016-06-30", 770_000_000, "2016-08-25")]},
                "NetIncomeLoss": {"USD": [_dur("2015-07-01", "2016-06-30", 115_000_000, "2016-08-25")]},
            }
        )
        annual_rows, _ = extract_annual_financials(cf)
        self.assertEqual(annual_rows[0]["revenue"], 2_750_000_000.0)

    def test_bank_nii_plus_noninterest_overrides_fee_only_revenue(self) -> None:
        """CPF/NBHC/ZION-style: Bank NII + Noninterest Income overrides partial fee revenue."""
        cf = _make_facts(
            {
                "Revenues": {"USD": [_dur("2025-01-01", "2025-12-31", 52_000_000, "2026-02-20")]},
                "InterestIncomeExpenseNet": {"USD": [_dur("2025-01-01", "2025-12-31", 240_000_000, "2026-02-20")]},
                "NoninterestIncome": {"USD": [_dur("2025-01-01", "2025-12-31", 52_000_000, "2026-02-20")]},
                "NetIncomeLoss": {"USD": [_dur("2025-01-01", "2025-12-31", 65_000_000, "2026-02-20")]},
            }
        )
        annual_rows, _ = extract_annual_financials(cf)
        self.assertEqual(annual_rows[0]["revenue"], 292_000_000.0)


class TestDividendValidation(unittest.TestCase):
    def test_single_quarter_dps_paid_rejected_while_valid_declared_kept(self) -> None:
        """AGO/AES-style: 1-quarter dps_paid (~1/4 implied) is dropped while annual dps_declared stays."""
        cf = _make_facts(
            {
                "Revenues": {"USD": [_dur("2025-01-01", "2025-12-31", 1_000_000_000, "2026-02-25")]},
                "NetIncomeLoss": {"USD": [_dur("2025-01-01", "2025-12-31", 300_000_000, "2026-02-25")]},
                "WeightedAverageNumberOfDilutedSharesOutstanding": {
                    "shares": [_dur("2025-01-01", "2025-12-31", 50_000_000, "2026-02-25")]
                },
                "PaymentsOfDividendsCommonStock": {
                    "USD": [_dur("2025-01-01", "2025-12-31", 68_000_000, "2026-02-25")]
                },
                # Implied DPS = 68M / 50M = $1.36; dps_paid mis-tagged as 1 quarter ($0.34 = 0.25x)
                "CommonStockDividendsPerShareDeclared": {
                    "USD/shares": [_dur("2025-01-01", "2025-12-31", 1.36, "2026-02-25")]
                },
                "CommonStockDividendsPerShareCashPaid": {
                    "USD/shares": [_dur("2025-01-01", "2025-12-31", 0.34, "2026-02-25")]
                },
            }
        )
        rows, _ = extract_annual_financials(cf)
        self.assertIsNone(rows[0]["dps_paid"])
        self.assertAlmostEqual(rows[0]["dps_declared"], 1.36, places=4)

    def test_single_quarter_dps_paid_and_declared_both_rejected(self) -> None:
        """TNET/AGM-style: both dps_paid and dps_declared mis-tagged as single quarter."""
        cf = _make_facts(
            {
                "Revenues": {"USD": [_dur("2025-01-01", "2025-12-31", 5_000_000_000, "2026-02-25")]},
                "NetIncomeLoss": {"USD": [_dur("2025-01-01", "2025-12-31", 200_000_000, "2026-02-25")]},
                "WeightedAverageNumberOfDilutedSharesOutstanding": {
                    "shares": [_dur("2025-01-01", "2025-12-31", 50_000_000, "2026-02-25")]
                },
                "PaymentsOfDividendsCommonStock": {
                    "USD": [_dur("2025-01-01", "2025-12-31", 53_000_000, "2026-02-25")]
                },
                # Implied DPS = $1.06; both tags report single-quarter $0.275
                "CommonStockDividendsPerShareDeclared": {
                    "USD/shares": [_dur("2025-01-01", "2025-12-31", 0.275, "2026-02-25")]
                },
                "CommonStockDividendsPerShareCashPaid": {
                    "USD/shares": [_dur("2025-01-01", "2025-12-31", 0.275, "2026-02-25")]
                },
            }
        )
        rows, _ = extract_annual_financials(cf)
        self.assertIsNone(rows[0]["dps_paid"])
        self.assertIsNone(rows[0]["dps_declared"])

    def test_stale_special_dividend_footnote_copy_paste_rejected(self) -> None:
        """ACGL-style: prior year's $5.00 special dividend copied verbatim into next FY despite cash drop."""
        cf = _make_facts(
            {
                "Revenues": {
                    "USD": [
                        _dur("2024-01-01", "2024-12-31", 15_000_000_000, "2025-02-25"),
                        _dur("2025-01-01", "2025-12-31", 16_000_000_000, "2026-02-25"),
                    ]
                },
                "NetIncomeLoss": {
                    "USD": [
                        _dur("2024-01-01", "2024-12-31", 4_000_000_000, "2025-02-25"),
                        _dur("2025-01-01", "2025-12-31", 4_200_000_000, "2026-02-25"),
                    ]
                },
                "WeightedAverageNumberOfDilutedSharesOutstanding": {
                    "shares": [
                        _dur("2024-01-01", "2024-12-31", 380_000_000, "2025-02-25"),
                        _dur("2025-01-01", "2025-12-31", 380_000_000, "2026-02-25"),
                    ]
                },
                "PaymentsOfDividendsCommonStock": {
                    "USD": [
                        _dur("2024-01-01", "2024-12-31", 1_900_000_000, "2025-02-25"),
                        _dur("2025-01-01", "2025-12-31", 10_000_000, "2026-02-25"),
                    ]
                },
                "CommonStockDividendsPerShareDeclared": {
                    "USD/shares": [
                        _dur("2024-01-01", "2024-12-31", 5.00, "2025-02-25"),
                        _dur("2025-01-01", "2025-12-31", 5.00, "2026-02-25"),
                    ]
                },
            }
        )
        rows, _ = extract_annual_financials(cf)
        by_end = {r["fy_end"]: r for r in rows}
        self.assertAlmostEqual(by_end["2024-12-31"]["dps_declared"], 5.00, places=4)
        self.assertIsNone(by_end["2025-12-31"]["dps_declared"])

    def test_partial_single_quarter_dividend_fallback_rejected(self) -> None:
        """SPG FY2019-style: 1-quarter PaymentsOfDividends sum (<50% of DPS*shares) is rejected."""
        cf = _make_facts(
            {
                "Revenues": {"USD": [_dur("2019-01-01", "2019-12-31", 5_700_000_000, "2020-02-25")]},
                "NetIncomeLoss": {"USD": [_dur("2019-01-01", "2019-12-31", 2_100_000_000, "2020-02-25")]},
                "WeightedAverageNumberOfDilutedSharesOutstanding": {
                    "shares": [_dur("2019-01-01", "2019-12-31", 308_000_000, "2020-02-25")]
                },
                "CommonStockDividendsPerShareDeclared": {
                    "USD/shares": [_dur("2019-01-01", "2019-12-31", 8.30, "2020-02-25")]
                },
                # Only Q1 cash dividend reported in 10-Q ($634M vs ~$2,556M annual)
                "PaymentsOfDividendsCommonStock": {
                    "USD": [_dur("2019-01-01", "2019-03-31", 634_000_000, "2019-05-03", "10-Q")]
                },
            }
        )
        rows, _ = extract_annual_financials(cf)
        self.assertIsNone(rows[0]["dividends_paid"])

    def test_full_year_quarterly_dividend_fallback_accepted(self) -> None:
        """AAPL FY2012-style: quarterly PaymentsOfDividends matching full-year DPS*shares is accepted."""
        cf = _make_facts(
            {
                "Revenues": {"USD": [_dur("2011-09-25", "2012-09-29", 156_500_000_000, "2012-10-31")]},
                "NetIncomeLoss": {"USD": [_dur("2011-09-25", "2012-09-29", 41_700_000_000, "2012-10-31")]},
                "WeightedAverageNumberOfDilutedSharesOutstanding": {
                    "shares": [_dur("2011-09-25", "2012-09-29", 945_000_000, "2012-10-31")]
                },
                "CommonStockDividendsPerShareDeclared": {
                    "USD/shares": [_dur("2011-09-25", "2012-09-29", 2.65, "2012-10-31")]
                },
                # Q4 initiation payment ~$2.5B matches 945M * $2.65 = $2.504B
                "PaymentsOfDividendsCommonStock": {
                    "USD": [_dur("2012-07-01", "2012-09-29", 2_500_000_000, "2012-10-31", "10-Q")]
                },
            }
        )
        rows, _ = extract_annual_financials(cf)
        self.assertEqual(rows[0]["dividends_paid"], 2_500_000_000.0)


class TestBalanceSheetAndEntities(unittest.TestCase):
    def test_q4_derived_revenue_rejected_when_q3_ytd_too_small(self) -> None:
        """CBSH/SFNC-style: Q4 = Annual - Q3 YTD is rejected when Q3 YTD <= 0 or < 30% of annual."""
        cf = _make_facts(
            {
                "Revenues": {
                    "USD": [
                        _dur("2025-01-01", "2025-12-31", 1_000_000_000, "2026-02-20"),
                        _dur("2025-01-01", "2025-09-30", 150_000_000, "2025-11-01", "10-Q"),
                    ]
                },
                "NetIncomeLoss": {
                    "USD": [
                        _dur("2025-01-01", "2025-12-31", 200_000_000, "2026-02-20"),
                        _dur("2025-01-01", "2025-09-30", 150_000_000, "2025-11-01", "10-Q"),
                    ]
                },
            }
        )
        annual_rows, splits = extract_annual_financials(cf)
        q_rows = extract_quarterly_financials(cf, annual_rows, splits)
        q4 = next(q for q in q_rows if q["period_end"] == "2025-12-31")
        self.assertIsNone(q4["revenue"])
        self.assertEqual(q4["net_income"], 50_000_000.0)
    def test_balance_sheet_rejects_negative_and_cash_gt_assets(self) -> None:
        """Reject cash > total_assets * 1.05 (IPAR 1000x typo), negative debt, and negative intangibles."""
        cf = _make_facts(
            {
                "Revenues": {"USD": [_dur("2021-01-01", "2021-12-31", 880_000_000, "2022-03-01")]},
                "NetIncomeLoss": {"USD": [_dur("2021-01-01", "2021-12-31", 120_000_000, "2022-03-01")]},
                "Assets": {"USD": [_inst("2021-12-31", 1_145_000_000, "2022-03-01")]},
                "CashAndCashEquivalentsAtCarryingValue": {
                    "USD": [_inst("2021-12-31", 159_613_000_000, "2022-03-01")]
                },
                "LongTermDebt": {"USD": [_inst("2021-12-31", -5_000_000, "2022-03-01")]},
                "IntangibleAssetsNetExcludingGoodwill": {
                    "USD": [_inst("2021-12-31", -25_000_000, "2022-03-01")]
                },
            }
        )
        rows, _ = extract_annual_financials(cf)
        self.assertEqual(rows[0]["total_assets"], 1_145_000_000.0)
        self.assertIsNone(rows[0]["cash"])
        self.assertIsNone(rows[0]["total_debt"])
        self.assertIsNone(rows[0]["intangibles"])

    def test_merge_company_facts_combines_predecessor_and_successor(self) -> None:
        old_cf = _make_facts(
            {"Revenues": {"USD": [_dur("2020-01-01", "2020-12-31", 100_000_000, "2021-02-20")]}}
        )
        new_cf = _make_facts(
            {"Revenues": {"USD": [_dur("2025-01-01", "2025-12-31", 200_000_000, "2026-02-20")]}}
        )
        merged = merge_company_facts([old_cf, new_cf])
        rows, _ = extract_annual_financials(merged)
        self.assertEqual([r["fy_end"] for r in rows], ["2020-12-31", "2025-12-31"])

    def test_is_non_common_ticker(self) -> None:
        for non_common in ("OXY.WS", "FOO.WT", "AHL.PD", "BAR.PR", "TMUSR"):
            self.assertTrue(is_non_common_ticker(non_common), non_common)
        for common in ("AAPL", "BRK.B", "BF.B", "CWEN.A", "LEN.B", "GOOGL"):
            self.assertFalse(is_non_common_ticker(common), common)


if __name__ == "__main__":
    unittest.main()
