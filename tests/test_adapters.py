"""Unit tests for SEC filing adapters (SPDR UIT N-30D legacy parser)."""

from __future__ import annotations

import unittest

from src.adapters.spdr_legacy import parse_spdr_n30d_html


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


if __name__ == "__main__":
    unittest.main()
