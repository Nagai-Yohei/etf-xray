"""Unit and in-memory SQLite integration tests for quarter_prices and stock_metrics."""

from __future__ import annotations

import sqlite3
import unittest
from typing import Any

from src.db import SCHEMA_SQL
from src.metrics import (
    _calc_cagr,
    _compute_dividend_streak,
    _effective_dps,
    _find_value_n_years_ago,
    rebuild_quarter_prices,
    rebuild_stock_metrics,
)


def _new_memory_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    conn.execute(
        "INSERT INTO etfs (etf_id, name, issuer, cik, series_id, structure) VALUES ('VOO', 'Vanguard S&P 500', 'Vanguard', '0000036405', 'S000002839', 'open_end')"
    )
    conn.execute(
        "INSERT INTO etfs (etf_id, name, issuer, cik, series_id, structure) VALUES ('VYM', 'Vanguard High Div', 'Vanguard', '0000857489', 'S000014209', 'open_end')"
    )
    return conn


def _insert_stock(
    conn: sqlite3.Connection,
    sec_id: str,
    sector_group: str | None = None,
    price: float = 100.0,
    price_date: str = "2026-06-30",
    annual_overrides: dict[str, Any] | None = None,
    ttm_overrides: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        "INSERT INTO securities (security_id, ticker, name, cik, sector_group, first_seen, last_seen) VALUES (?, ?, ?, NULL, ?, ?, ?)",
        (sec_id, sec_id, f"{sec_id} Corp", sector_group, "2020-01-01", "2020-01-01"),
    )
    conn.execute(
        "INSERT INTO etf_dates (etf_id, as_of, holdings_count, total_value_usd, source) VALUES ('VOO', ?, 1, ?, 'N-PORT-P') ON CONFLICT DO NOTHING",
        (price_date, price * 1_000_000),
    )
    conn.execute(
        "INSERT INTO etf_holdings (etf_id, as_of, security_id, reported_name, shares, value_usd, weight, rank) VALUES ('VOO', ?, ?, ?, 1000000, ?, 1.0, 1)",
        (price_date, sec_id, f"{sec_id} Corp", price * 1_000_000),
    )

    ann = {
        "fy_end": "2025-12-31",
        "fy_start": "2025-01-01",
        "filed": "2026-02-20",
        "revenue": 10_000_000_000.0,
        "gross_profit": 4_000_000_000.0,
        "operating_income": 2_000_000_000.0,
        "net_income": 1_500_000_000.0,
        "interest_expense": 200_000_000.0,
        "eps_diluted": 5.0,
        "shares_diluted": 300_000_000.0,
        "shares_outstanding": 300_000_000.0,
        "operating_cf": 2_200_000_000.0,
        "capex": 400_000_000.0,
        "fcf": 1_800_000_000.0,
        "sbc": 100_000_000.0,
        "dividends_paid": 600_000_000.0,
        "total_assets": 20_000_000_000.0,
        "total_equity": 8_000_000_000.0,
        "cash": 2_000_000_000.0,
        "total_debt": 4_000_000_000.0,
        "dps_declared": 2.0,
        "dps_paid": 2.0,
        "split_factor": 1.0,
    }
    if annual_overrides:
        ann.update(annual_overrides)
    cols = ", ".join(["security_id", *ann.keys()])
    qs = ", ".join(["?"] * (len(ann) + 1))
    conn.execute(f"INSERT INTO financials_annual ({cols}) VALUES ({qs})", [sec_id, *ann.values()])

    ttm = {
        "period_end": "2026-06-30",
        "revenue_ttm": 10_500_000_000.0,
        "operating_income_ttm": 2_100_000_000.0,
        "net_income_ttm": 1_600_000_000.0,
        "eps_ttm": 5.2,
        "fcf_ttm": 1_900_000_000.0,
        "dps_ttm": 2.1,
        "total_equity": 8_200_000_000.0,
        "shares_outstanding": 300_000_000.0,
    }
    if ttm_overrides:
        ttm.update(ttm_overrides)
    tcols = ", ".join(["security_id", *ttm.keys()])
    tqs = ", ".join(["?"] * (len(ttm) + 1))
    conn.execute(f"INSERT INTO financials_ttm ({tcols}) VALUES ({tqs})", [sec_id, *ttm.values()])


class TestMetricHelpers(unittest.TestCase):
    def test_effective_dps_priority_and_multiclass_fallback(self) -> None:
        self.assertEqual(
            _effective_dps({"dps_declared": 1.2, "dps_paid": 1.0, "dividends_paid": 500, "shares_diluted": 100, "shares_outstanding": 100}),
            (1.2, False),
        )
        self.assertEqual(
            _effective_dps({"dps_declared": None, "dps_paid": 1.0, "dividends_paid": 500, "shares_diluted": 100, "shares_outstanding": 100}),
            (1.0, False),
        )
        # Dual-class issuer (CWEN-style): shares_outstanding >> shares_diluted uses total shares_outstanding
        val, is_fb = _effective_dps(
            {"dps_declared": None, "dps_paid": None, "dividends_paid": 200_000_000.0, "shares_diluted": 35_000_000.0, "shares_outstanding": 200_000_000.0}
        )
        self.assertTrue(is_fb)
        self.assertAlmostEqual(val, 1.0, places=4)

    def test_calc_cagr_and_n_years_ago(self) -> None:
        self.assertAlmostEqual(_calc_cagr(1.0, 2.0, 5), 2.0 ** 0.2 - 1.0, places=6)
        self.assertIsNone(_calc_cagr(0.0, 2.0, 5))
        self.assertIsNone(_calc_cagr(-1.0, 2.0, 5))

        series = [
            ("2019-12-29", 100.0),
            ("2020-12-27", 110.0),
            ("2021-12-26", 120.0),
            ("2025-12-28", 180.0),
        ]
        # 52/53-week fiscal calendar ~5 years prior to 2025-12-28 picks 2020-12-27 (not 2019 or 2021)
        val, span = _find_value_n_years_ago(series, "2025-12-28", 5)
        self.assertEqual(val, 110.0)
        self.assertEqual(span, 5)
        # Missing 10-year anchor returns None
        self.assertEqual(_find_value_n_years_ago(series, "2025-12-28", 10), (None, 0))

    def test_dividend_streak_open_broken_and_tolerance(self) -> None:
        rows = [
            {"dps_declared": 1.00, "dps_paid": None, "dividends_paid": None, "shares_diluted": None, "shares_outstanding": None},
            {"dps_declared": 0.99, "dps_paid": None, "dividends_paid": None, "shares_diluted": None, "shares_outstanding": None},  # within 1.5% tolerance
            {"dps_declared": 1.05, "dps_paid": None, "dividends_paid": None, "shares_diluted": None, "shares_outstanding": None},
        ]
        streak, is_open, _ = _compute_dividend_streak(rows)
        self.assertEqual((streak, is_open), (3, 1))

        cut_rows = [
            {"dps_declared": 2.00, "dps_paid": None, "dividends_paid": None, "shares_diluted": None, "shares_outstanding": None},
            {"dps_declared": 1.00, "dps_paid": None, "dividends_paid": None, "shares_diluted": None, "shares_outstanding": None},
            {"dps_declared": 1.10, "dps_paid": None, "dividends_paid": None, "shares_diluted": None, "shares_outstanding": None},
        ]
        streak2, is_open2, _ = _compute_dividend_streak(cut_rows)
        self.assertEqual((streak2, is_open2), (2, 0))


class TestMetricsIntegration(unittest.TestCase):
    def test_sector_omissions_and_guards(self) -> None:
        conn = _new_memory_db()
        _insert_stock(conn, "CORP", sector_group=None)
        _insert_stock(conn, "REIT1", sector_group="reit")
        _insert_stock(conn, "BANK1", sector_group="bank")
        _insert_stock(conn, "INS1", sector_group="insurance")
        _insert_stock(conn, "AM1", sector_group="asset_mgmt")
        _insert_stock(conn, "NEGEQ", sector_group=None, ttm_overrides={"total_equity": -500_000_000.0})
        _insert_stock(conn, "STALE", sector_group=None, price_date="2022-06-30")
        _insert_stock(conn, "OXY.WS", sector_group=None)

        rebuild_quarter_prices(conn)
        rebuild_stock_metrics(conn)

        rows = {
            r["security_id"]: dict(r)
            for r in conn.execute("SELECT * FROM stock_metrics").fetchall()
        }

        # Standard corporate has all core ratios
        self.assertIsNotNone(rows["CORP"]["payout_ratio_eps"])
        self.assertIsNotNone(rows["CORP"]["roic"])
        self.assertIsNotNone(rows["CORP"]["roe"])
        self.assertIsNotNone(rows["CORP"]["pe_ratio"])

        # REIT omits GAAP EPS and FCF payout ratios
        self.assertIsNone(rows["REIT1"]["payout_ratio_eps"])
        self.assertIsNone(rows["REIT1"]["payout_ratio_fcf"])
        self.assertIsNotNone(rows["REIT1"]["ttm_yield"])

        # Bank omits ROIC, NetDebt/FCF, FCF margins, and FCF payout
        self.assertIsNone(rows["BANK1"]["roic"])
        self.assertIsNone(rows["BANK1"]["net_debt_to_fcf"])
        self.assertIsNone(rows["BANK1"]["fcf_margin"])
        self.assertIsNone(rows["BANK1"]["payout_ratio_fcf"])

        # Insurer also omits interest_coverage
        self.assertIsNone(rows["INS1"]["roic"])
        self.assertIsNone(rows["INS1"]["interest_coverage"])

        # Asset manager omits ROIC and NetDebt/FCF
        self.assertIsNone(rows["AM1"]["roic"])
        self.assertIsNone(rows["AM1"]["net_debt_to_fcf"])

        # Negative TTM equity omits ROE and P/B
        self.assertIsNone(rows["NEGEQ"]["roe"])
        self.assertIsNone(rows["NEGEQ"]["pb_ratio"])

        # Stale price predating latest financials omits price-based valuation/yield ratios
        self.assertIsNone(rows["STALE"]["pe_ratio"])
        self.assertIsNone(rows["STALE"]["pb_ratio"])
        self.assertIsNone(rows["STALE"]["ttm_yield"])

        # Warrant omits common-stock metrics
        self.assertIsNone(rows["OXY.WS"]["pe_ratio"])
        self.assertIsNone(rows["OXY.WS"]["ttm_yield"])

        # first_seen / last_seen synced strictly to etf_holdings
        sec = conn.execute("SELECT first_seen, last_seen FROM securities WHERE security_id='CORP'").fetchone()
        self.assertEqual((sec["first_seen"], sec["last_seen"]), ("2026-06-30", "2026-06-30"))
        conn.close()

    def test_roe_5y_median_and_fcf_growth_metrics(self) -> None:
        conn = _new_memory_db()
        _insert_stock(conn, "GROW", sector_group=None)
        _insert_stock(conn, "BANK2", sector_group="bank")

        # Replace single annual row with 6 years of history (FY2020..FY2025)
        for sec_id in ("GROW", "BANK2"):
            conn.execute("DELETE FROM financials_annual WHERE security_id = ?", (sec_id,))
            yearly = [
                ("2020-12-31", 100.0, 500.0, 100.0),   # ROE 20%, FCF 100
                ("2021-12-31", 120.0, 600.0, -10.0),   # ROE 20%, FCF -10 (breaks streak earlier)
                ("2022-12-31", 150.0, 600.0, 110.0),   # ROE 25%, FCF 110
                ("2023-12-31", 180.0, 600.0, 130.0),   # ROE 30%, FCF 130
                ("2024-12-31", 140.0, 700.0, 150.0),   # ROE 20%, FCF 150
                ("2025-12-31", 175.0, 700.0, 161.051), # ROE 25%, FCF 161.051 (+10% 5Y CAGR from 100)
            ]
            for fy, ni, eq, fcf in yearly:
                conn.execute(
                    """
                    INSERT INTO financials_annual (
                        security_id, fy_end, revenue, net_income, total_equity, fcf, split_factor
                    ) VALUES (?, ?, 1000.0, ?, ?, ?, 1.0)
                    """,
                    (sec_id, fy, ni, eq, fcf),
                )

        rebuild_quarter_prices(conn)
        rebuild_stock_metrics(conn)
        rows = {
            r["security_id"]: r
            for r in conn.execute("SELECT * FROM stock_metrics").fetchall()
        }

        # Standard corporate: last 5 ROEs = [20%, 25%, 30%, 20%, 25%] -> median 25%
        self.assertAlmostEqual(rows["GROW"]["roe_5y_median"], 0.25, places=4)
        self.assertAlmostEqual(rows["GROW"]["fcf_cagr_5y"], 0.10, places=3)
        self.assertEqual(rows["GROW"]["fcf_positive_years"], 4)

        # Bank retains 5Y median ROE, suppresses FCF growth and streak
        self.assertAlmostEqual(rows["BANK2"]["roe_5y_median"], 0.25, places=4)
        self.assertIsNone(rows["BANK2"]["fcf_cagr_5y"])
        self.assertIsNone(rows["BANK2"]["fcf_positive_years"])
        conn.close()

    def test_roe_and_roic_small_denominator_and_extreme_guards(self) -> None:
        conn = _new_memory_db()
        # Near-zero equity buyback case (e.g. CL: eq/assets = 0.3%, raw ROE = 3900%)
        _insert_stock(
            conn,
            "TINYEQ",
            annual_overrides={
                "total_assets": 15_000_000_000.0,
                "total_equity": 50_000_000.0,
                "net_income": 2_000_000_000.0,
                "operating_income": 2_500_000_000.0,
                "total_debt": 7_000_000_000.0,
                "cash": 1_000_000_000.0,
            },
            ttm_overrides={"total_equity": 50_000_000.0},
        )
        # Near-zero invested capital case (e.g. NPWR/STXS: cash nearly cancels equity+debt)
        _insert_stock(
            conn,
            "TINYIC",
            annual_overrides={
                "total_assets": 500_000_000.0,
                "total_equity": 200_000_000.0,
                "total_debt": 50_000_000.0,
                "cash": 249_500_000.0,  # invested_cap = $500k (0.1% of assets)
                "operating_income": -100_000_000.0,
                "net_income": -90_000_000.0,
            },
            ttm_overrides={"total_equity": 200_000_000.0},
        )
        # Normal high-capital-efficiency company (inv/assets = 4%, ROE = 25%, ROIC = 35%) stays intact
        _insert_stock(
            conn,
            "HEALTHY",
            annual_overrides={
                "total_assets": 100_000_000_000.0,
                "total_equity": 8_000_000_000.0,
                "total_debt": 2_000_000_000.0,
                "cash": 6_000_000_000.0,  # invested_cap = $4B (4% of assets)
                "operating_income": 1_800_000_000.0,
                "net_income": 2_000_000_000.0,
            },
            ttm_overrides={"total_equity": 8_000_000_000.0},
        )

        rebuild_quarter_prices(conn)
        rebuild_stock_metrics(conn)
        rows = {
            r["security_id"]: r
            for r in conn.execute("SELECT * FROM stock_metrics").fetchall()
        }

        self.assertIsNone(rows["TINYEQ"]["roe"])
        self.assertIsNotNone(rows["TINYEQ"]["roic"])
        self.assertIn("ROE omitted", rows["TINYEQ"]["data_notes"] or "")

        self.assertIsNotNone(rows["TINYIC"]["roe"])
        self.assertIsNone(rows["TINYIC"]["roic"])
        self.assertIn("ROIC omitted", rows["TINYIC"]["data_notes"] or "")

        self.assertAlmostEqual(rows["HEALTHY"]["roe"], 0.25, places=4)
        self.assertIsNotNone(rows["HEALTHY"]["roic"])
        self.assertGreater(rows["HEALTHY"]["roic"], 0.25)
        conn.close()

    def test_persisted_split_events_adjust_post_price_split_without_disk_cache(self) -> None:
        conn = _new_memory_db()
        _insert_stock(
            conn,
            "SPLT",
            price=200.0,
            price_date="2026-06-30",
            annual_overrides={"split_factor": 2.0, "eps_diluted": 5.0, "dps_declared": 2.0},
            ttm_overrides={"eps_ttm": 5.0, "dps_ttm": 2.0},
        )
        conn.execute(
            "INSERT INTO split_events (security_id, filed, ratio) VALUES ('SPLT', '2026-08-01', 2.0)"
        )
        conn.commit()

        rebuild_quarter_prices(conn)
        rebuild_stock_metrics(conn)
        row = conn.execute("SELECT * FROM stock_metrics WHERE security_id = 'SPLT'").fetchone()

        # Pre-split implied price ($200) aligned to post-split basis ($100): P/E = 100 / 5 = 20x, yield = 2 / 100 = 2%
        self.assertAlmostEqual(row["pe_ratio"], 20.0, places=3)
        self.assertAlmostEqual(row["ttm_yield"], 0.02, places=4)
        self.assertIn("adjusted for stock split", row["data_notes"] or "")
        conn.close()


if __name__ == "__main__":
    unittest.main()
