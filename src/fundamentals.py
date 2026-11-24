"""Public facade and CLI for SEC EDGAR XBRL financial/dividend extraction.

Implementation is split by responsibility under src/xbrl/:
- src/xbrl/tags.py      : Tag priority maps, form constants, and CIK override/merge helpers
- src/xbrl/records.py   : FactRecord parsing, period selection, share-scale & debt helpers
- src/xbrl/splits.py    : Stock-split inference from 10-K/10-Q comparative periods
- src/xbrl/annual.py    : Annual financials extraction, revenue normalization, DPS validation
- src/xbrl/quarterly.py : TTM and standalone quarterly extraction (including Q4 derivation)
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict

from src.edgar import EdgarClient, normalize_cik
from src.xbrl.annual import extract_annual_financials
from src.xbrl.quarterly import extract_quarterly_financials, extract_ttm_financials
from src.xbrl.records import FactRecord
from src.xbrl.tags import (
    ANNUAL_FORMS,
    BANK_NII_TAGS,
    BANK_NONINT_TAGS,
    CIK_OVERRIDES_PATH,
    KNOWN_SPLIT_RATIOS,
    QUARTERLY_FORMS,
    REIT_LEASE_TAGS,
    TAG_MAP_FLOW_PER_SHARE,
    TAG_MAP_FLOW_SHARES,
    TAG_MAP_FLOW_USD,
    TAG_MAP_INSTANT_SHARES,
    TAG_MAP_INSTANT_USD,
    get_cik_chain,
    load_cik_overrides,
    merge_company_facts,
)

__all__ = [
    "ANNUAL_FORMS",
    "BANK_NII_TAGS",
    "BANK_NONINT_TAGS",
    "CIK_OVERRIDES_PATH",
    "FactRecord",
    "KNOWN_SPLIT_RATIOS",
    "QUARTERLY_FORMS",
    "REIT_LEASE_TAGS",
    "TAG_MAP_FLOW_PER_SHARE",
    "TAG_MAP_FLOW_SHARES",
    "TAG_MAP_FLOW_USD",
    "TAG_MAP_INSTANT_SHARES",
    "TAG_MAP_INSTANT_USD",
    "extract_annual_financials",
    "extract_quarterly_financials",
    "extract_ttm_financials",
    "get_cik_chain",
    "load_cik_overrides",
    "merge_company_facts",
]


def _fmt_billions(val: float | None) -> str:
    if val is None:
        return "-"
    return f"{val / 1e9:7.2f}B"


def _fmt_num(val: float | None) -> str:
    if val is None:
        return "-"
    return f"{val:6.2f}"


def verify_sample_tickers(tickers: list[str]) -> int:
    """Print a verification table of SEC XBRL extraction results for the given tickers."""
    client = EdgarClient()
    ticker_to_cik = client.build_ticker_to_cik_map()

    print(f"Tickers verified: {len(tickers)}")
    print("=" * 116)
    print(
        f"{'Ticker':<6} {'CIK':<10} {'Yrs':>3} {'LatestFY':<10} "
        f"{'Rev':>8} {'OpInc':>8} {'NetInc':>8} {'FCF':>8} "
        f"{'EPS':>6} {'DPS_d':>6} {'DPS_p':>6} {'DivPaid':>8} {'Splits'}"
    )
    print("-" * 116)

    missing_summary: dict[str, list[str]] = defaultdict(list)

    for raw_ticker in tickers:
        ticker = raw_ticker.upper()
        cik = ticker_to_cik.get(ticker)
        if not cik:
            print(f"{ticker:<6} {'NOT FOUND':<10}")
            continue

        facts = client.get_company_facts(cik)
        rows, splits = extract_annual_financials(facts)
        if not rows:
            print(f"{ticker:<6} {normalize_cik(cik):<10} {'0':>3} NO ANNUAL DATA")
            continue

        latest = rows[-1]
        for field in (
            "revenue",
            "operating_income",
            "net_income",
            "eps_diluted",
            "operating_cf",
            "capex",
            "fcf",
            "dividends_paid",
            "total_assets",
            "total_equity",
            "total_debt",
            "dps_declared",
            "dps_paid",
        ):
            if latest[field] is None:
                missing_summary[field].append(ticker)

        split_str = ", ".join(f"{dt}:{r:g}x" for dt, r in splits) if splits else "-"
        print(
            f"{ticker:<6} {cik:<10} {len(rows):>3} {latest['fy_end']:<10} "
            f"{_fmt_billions(latest['revenue']):>8} "
            f"{_fmt_billions(latest['operating_income']):>8} "
            f"{_fmt_billions(latest['net_income']):>8} "
            f"{_fmt_billions(latest['fcf']):>8} "
            f"{_fmt_num(latest['eps_diluted']):>6} "
            f"{_fmt_num(latest['dps_declared']):>6} "
            f"{_fmt_num(latest['dps_paid']):>6} "
            f"{_fmt_billions(latest['dividends_paid']):>8} "
            f"{split_str}"
        )

    print("=" * 116)
    print("Summary of missing fields in latest fiscal year:")
    for field, tlist in sorted(missing_summary.items()):
        print(f"  - {field:<18}: {len(tlist):>2} ticker(s) ({', '.join(tlist)})")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="SEC XBRL fundamentals extractor")
    parser.add_argument(
        "--verify",
        nargs="+",
        help="Verify extraction for space-separated tickers (e.g. KO PEP AVGO FAST)",
    )
    parser.add_argument(
        "--detail",
        help="Print full annual history for a single ticker",
    )
    args = parser.parse_args()

    if args.detail:
        client = EdgarClient()
        cik = client.build_ticker_to_cik_map().get(args.detail.upper())
        if not cik:
            print(f"CIK not found for {args.detail}", file=sys.stderr)
            return 1
        facts = client.get_company_facts(cik)
        rows, splits = extract_annual_financials(facts)
        print(f"=== {args.detail.upper()} (CIK {cik}) | Splits: {splits} ===")
        print(
            f"{'FY End':<10} {'Filed':<10} {'Rev':>8} {'OpInc':>8} {'NetInc':>8} "
            f"{'FCF':>8} {'EPS':>6} {'DPS_d':>6} {'DPS_p':>6} {'DivPaid':>8} {'Equity':>8} {'Debt':>8} {'Split':>6}"
        )
        for r in rows:
            print(
                f"{r['fy_end']:<10} {(r['filed'] or '-'):<10} "
                f"{_fmt_billions(r['revenue']):>8} "
                f"{_fmt_billions(r['operating_income']):>8} "
                f"{_fmt_billions(r['net_income']):>8} "
                f"{_fmt_billions(r['fcf']):>8} "
                f"{_fmt_num(r['eps_diluted']):>6} "
                f"{_fmt_num(r['dps_declared']):>6} "
                f"{_fmt_num(r['dps_paid']):>6} "
                f"{_fmt_billions(r['dividends_paid']):>8} "
                f"{_fmt_billions(r['total_equity']):>8} "
                f"{_fmt_billions(r['total_debt']):>8} "
                f"{r['split_factor']:>6.2f}"
            )
        return 0

    if args.verify:
        return verify_sample_tickers(args.verify)

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
