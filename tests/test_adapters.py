"""Unit tests for SEC filing adapters (SPDR N-30D, Invesco N-30B-2, and Vanguard legacy HTML parsers)."""

from __future__ import annotations

import unittest

from src.adapters.invesco_legacy import parse_invesco_n30b2_html
from src.adapters.spdr_legacy import parse_spdr_n30d_html
from src.adapters.vanguard_legacy import parse_vanguard_legacy_html


class TestSpdrLegacyAdapter(unittest.TestCase):
    def test_parses_valid_n30d_table_and_reconciles_total(self) -> None:
        html = b"""
        <html><body>
        <table>
          <tr><th>Security Description</th><th>Shares</th><th>Value</th></tr>
          <tr><td>Common Stocks &mdash; 99.9%</td><td></td><td></td></tr>
          <tr><td>Apple, Inc. (a)</td><td>1,000</td><td>$</td><td>200,000</td></tr>
          <tr><td>Boeing Co.</td><td>1,000</td><td></td><td>300,000</td></tr>
          <tr><td>Total Common Stocks (Cost $450,000)</td><td></td><td>$</td><td>500,000</td></tr>
        </table>
        </body></html>
        """
        filing = parse_spdr_n30d_html(
            html_bytes=html,
            etf_id="DIA",
            as_of="2019-04-30",
            accession="0001193125-19-175597",
            filed="2019-06-18",
        )
        self.assertEqual(filing.etf_id, "DIA")
        self.assertEqual(filing.source, "N-30D")
        self.assertEqual(len(filing.holdings), 2)
        self.assertEqual(filing.holdings[0].reported_name, "Boeing Co.")
        self.assertEqual(filing.holdings[1].reported_name, "Apple, Inc.")
        self.assertEqual(filing.total_value_usd, 500_000.0)

    def test_raises_on_reconciliation_mismatch(self) -> None:
        html = b"""
        <html><body>
        <table>
          <tr><th>Security Description</th><th>Shares</th><th>Value</th></tr>
          <tr><td>Common Stocks</td><td></td><td></td></tr>
          <tr><td>Apple, Inc.</td><td>1,000</td><td>200,000</td></tr>
          <tr><td>Total Common Stocks</td><td></td><td>999,999</td></tr>
        </table>
        </body></html>
        """
        with self.assertRaises(ValueError):
            parse_spdr_n30d_html(
                html_bytes=html,
                etf_id="DIA",
                as_of="2019-04-30",
                accession="0001193125-19-175597",
                filed="2019-06-18",
            )


class TestVanguardLegacyAdapter(unittest.TestCase):
    def test_parses_100pct_equity_schedule_ending_with_total_investments(self) -> None:
        html = b"""
        <html><body>
        <p>Dividend Appreciation Index Fund</p>
        <p>Statement of Net Assets As of July 31, 2011</p>
        <table>
          <tr><th>Security</th><th>Shares</th><th>Market Value ($000)</th></tr>
          <tr><td>Common Stocks (100.0%)</td><td></td><td></td></tr>
          <tr><td>Coca-Cola Co.</td><td>5,000,000</td><td>350,000</td></tr>
          <tr><td>PepsiCo Inc.</td><td>4,000,000</td><td>250,000</td></tr>
          <tr><td>Total Investments (100.0%) (Cost $500,000)</td><td></td><td>600,000</td></tr>
        </table>
        </body></html>
        """
        filing = parse_vanguard_legacy_html(
            html_bytes=html,
            etf_id="VIG",
            fund_title="Dividend Appreciation Index Fund",
            as_of="2011-07-31",
            form="N-CSRS",
            accession="0000932471-11-003729",
            filed="2011-09-29",
        )
        self.assertEqual(len(filing.holdings), 2)
        self.assertEqual(filing.total_value_usd, 600_000_000.0)

    def test_rejects_total_investments_after_temporary_cash_section(self) -> None:
        html = b"""
        <html><body>
        <p>Dividend Appreciation Index Fund</p>
        <p>Statement of Net Assets As of July 31, 2011</p>
        <table>
          <tr><th>Security</th><th>Shares</th><th>Market Value ($000)</th></tr>
          <tr><td>Common Stocks (99.0%)</td><td></td><td></td></tr>
          <tr><td>Coca-Cola Co.</td><td>5,000,000</td><td>350,000</td></tr>
          <tr><td>Temporary Cash Investment (1.0%)</td><td></td><td></td></tr>
          <tr><td>Vanguard Market Liquidity Fund</td><td>3,500</td><td>3,500</td></tr>
          <tr><td>Total Investments (100.0%)</td><td></td><td>353,500</td></tr>
        </table>
        </body></html>
        """
        with self.assertRaises(ValueError):
            parse_vanguard_legacy_html(
                html_bytes=html,
                etf_id="VIG",
                fund_title="Dividend Appreciation Index Fund",
                as_of="2011-07-31",
                form="N-CSRS",
                accession="0000932471-11-003729",
                filed="2011-09-29",
            )


class TestInvescoLegacyAdapter(unittest.TestCase):
    def test_parses_2011_layout_name_shares_value(self) -> None:
        html = b"""
        <html><body>
        <table>
          <tr><th>Common Stock</th><th>Shares</th><th>Value</th></tr>
          <tr><td>Apple, Inc.*</td><td>1,000</td><td>$</td><td>400,000</td></tr>
          <tr><td>Microsoft Corp.</td><td>2,000</td><td></td><td>600,000</td></tr>
          <tr><td>Total Investments (Cost $800,000)</td><td></td><td></td><td>1,000,000</td></tr>
        </table>
        </body></html>
        """
        filing = parse_invesco_n30b2_html(
            html_bytes=html,
            etf_id="QQQ",
            as_of="2011-09-30",
            accession="0001104659-12-003105",
            filed="2012-01-20",
        )
        self.assertEqual(filing.source, "N-30B-2")
        self.assertEqual(len(filing.holdings), 2)
        self.assertEqual(filing.holdings[0].reported_name, "Microsoft Corp.")
        self.assertEqual(filing.holdings[1].reported_name, "Apple, Inc.")
        self.assertEqual(filing.total_value_usd, 1_000_000.0)

    def test_parses_2016_layout_shares_name_value(self) -> None:
        html = b"""
        <html><body>
        <table>
          <tr><th>Number of Shares</th><th></th><th>Value</th></tr>
          <tr><td>Common Stocks and Other Equity Interests&#8212;100.0%</td></tr>
          <tr><td>Internet&#8212;100.0%</td></tr>
          <tr><td>1,000</td><td>Alphabet, Inc., Class A(a)</td><td>$</td><td>800,000</td></tr>
          <tr><td>2,000</td><td>Amazon.com, Inc.(b)</td><td></td><td>1,200,000</td></tr>
          <tr><td>Total Investments in Securities(Cost $1,500,000)&#8212;100.0%</td><td></td><td>2,000,000</td></tr>
        </table>
        </body></html>
        """
        filing = parse_invesco_n30b2_html(
            html_bytes=html,
            etf_id="QQQ",
            as_of="2018-09-30",
            accession="0001193125-18-355790",
            filed="2018-12-21",
        )
        self.assertEqual(len(filing.holdings), 2)
        self.assertEqual(filing.holdings[0].reported_name, "Amazon.com, Inc.")
        self.assertEqual(filing.holdings[1].reported_name, "Alphabet, Inc., Class A")
        self.assertEqual(filing.total_value_usd, 2_000_000.0)

    def test_raises_on_reconciliation_mismatch(self) -> None:
        html = b"""
        <html><body>
        <table>
          <tr><th>Common Stock</th><th>Shares</th><th>Value</th></tr>
          <tr><td>Apple, Inc.*</td><td>1,000</td><td>400,000</td></tr>
          <tr><td>Total Investments (Cost $300,000)</td><td></td><td>999,999</td></tr>
        </table>
        </body></html>
        """
        with self.assertRaises(ValueError):
            parse_invesco_n30b2_html(
                html_bytes=html,
                etf_id="QQQ",
                as_of="2011-09-30",
                accession="0001104659-12-003105",
                filed="2012-01-20",
            )


if __name__ == "__main__":
    unittest.main()
