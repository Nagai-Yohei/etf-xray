"""Invesco UIT legacy filings (Form N-30B-2) HTML adapter (e.g. QQQ Sep 2011 - Sep 2018).

Extracts the full Common Stocks Schedule of Investments tables across both historical
Invesco QQQ N-30B-2 layouts:
  - 2011-2015: [Security Name, Shares, Value] under "Common Stock / Shares / Value"
  - 2016-2018: [Shares, Security Name, Value] under "Common Stocks and Other Equity Interests"

Reconciles every filing strictly against its reported "Total Investments" /
"Total Investments in Securities" line (exact whole-dollar tolerance <= $1 per stock).
"""

from __future__ import annotations

import re
from typing import Any

from lxml import html as lhtml

from src.adapters.nport import FilingHoldings, RawHolding
from src.edgar import EdgarClient, normalize_cik

_SPACE_RE = re.compile(r"\s+")
_NUM_RE = re.compile(r"^\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)$")
# Strip trailing footnote markers (e.g. *, (a), (b), (a)(b), †, ‡) while preserving Class A/B/C and Series A
_FOOTNOTE_SUFFIX_RE = re.compile(r"(?:\s*(?:\([a-z0-9,\s]+\)|[*†‡§^]))+$")
_PAREN_CLASS_RE = re.compile(r"\(\s*(Class\s+[A-Z]|Series\s+[A-Z])\s*\)", re.IGNORECASE)


def _clean_text(text: str) -> str:
    cleaned = (
        text.replace("\xa0", " ")
        .replace("\u200b", "")
        .replace("\x92", "'")
        .replace("\x91", "'")
        .replace("\x93", '"')
        .replace("\x94", '"')
        .replace("\x96", "-")
        .replace("\x97", "-")
        .replace("\u2014", "-")
        .replace("\u2013", "-")
    )
    return _SPACE_RE.sub(" ", cleaned).strip()


def _parse_number(cell: str) -> float | None:
    m = _NUM_RE.match(cell.strip())
    if not m:
        return None
    return float(m.group(1).replace(",", ""))


def _clean_security_name(raw_name: str) -> str:
    name = _clean_text(raw_name)
    name = _PAREN_CLASS_RE.sub(r"\1", name)
    prev = None
    while prev != name:
        prev = name
        name = _FOOTNOTE_SUFFIX_RE.sub("", name).strip()
    return _SPACE_RE.sub(" ", name).strip()


def parse_invesco_n30b2_html(
    html_bytes: bytes,
    etf_id: str,
    as_of: str,
    accession: str,
    filed: str,
) -> FilingHoldings:
    """Parse an Invesco QQQ Form N-30B-2 Schedule of Investments with strict total reconciliation."""
    tree = lhtml.fromstring(html_bytes)

    in_schedule = False
    layout: str | None = None  # "name_shares_val" (2011-2015) or "shares_name_val" (2016-2018)
    reported_total: float | None = None
    merged: dict[str, RawHolding] = {}

    for tbl in tree.xpath("//table"):
        if reported_total is not None:
            break

        tbl_rows = tbl.xpath(".//tr")
        # Quick pre-check before entering the schedule to skip non-holdings tables
        if not in_schedule:
            tbl_low = _clean_text(tbl.text_content()).lower()
            if "common stock" not in tbl_low or "value" not in tbl_low:
                continue

        for tr in tbl_rows:
            raw_cells = [_clean_text(td.text_content()) for td in tr.xpath("./td|./th")]
            cells = [c for c in raw_cells if c and c != "$"]
            if not cells:
                continue

            row_low = " ".join(cells).lower()
            first_low = cells[0].lower()

            # Detect schedule start & column layout
            if not in_schedule:
                if (
                    len(cells) >= 3
                    and "common stock" in first_low
                    and "shares" in cells[1].lower()
                    and "value" in cells[-1].lower()
                ):
                    in_schedule = True
                    layout = "name_shares_val"
                    continue
                if first_low.startswith("common stocks and other equity interests"):
                    in_schedule = True
                    layout = "shares_name_val"
                    continue
                continue

            # Detect terminating Total Investments row
            if first_low.startswith(("total investments", "total common stock")) and "at cost" not in first_low:
                for c in reversed(cells[1:]):
                    num = _parse_number(c)
                    if num is not None and num > 0:
                        reported_total = num
                        break
                if reported_total is not None:
                    break

            # Skip repeated page headers, continued lines, industry headers, and subtotals
            if (
                first_low.startswith(
                    (
                        "common stock",
                        "number of shares",
                        "numberof shares",
                        "security",
                        "see accompanying",
                    )
                )
                or len(cells) < 3
            ):
                continue

            name: str | None = None
            shares_val: float | None = None
            value_usd: float | None = None

            if layout == "name_shares_val":
                sh = _parse_number(cells[-2])
                val = _parse_number(cells[-1])
                if sh is not None and val is not None and sh > 0 and val > 0:
                    cand_name = _clean_security_name(" ".join(cells[:-2]))
                    if cand_name and _parse_number(cand_name) is None:
                        name, shares_val, value_usd = cand_name, sh, val
            elif layout == "shares_name_val":
                sh = _parse_number(cells[0])
                val = _parse_number(cells[-1])
                if sh is not None and val is not None and sh > 0 and val > 0:
                    cand_name = _clean_security_name(" ".join(cells[1:-1]))
                    if cand_name and _parse_number(cand_name) is None:
                        name, shares_val, value_usd = cand_name, sh, val

            if not name or shares_val is None or value_usd is None:
                continue
            if name.lower().startswith(("total ", "subtotal ", "cost ")):
                continue

            key = name.upper()
            if key in merged:
                merged[key].shares += shares_val
                merged[key].value_usd += value_usd
            else:
                merged[key] = RawHolding(
                    reported_name=name,
                    cusip=None,
                    isin=None,
                    shares=shares_val,
                    value_usd=value_usd,
                    pct_net_assets=None,
                )

    holdings = sorted(merged.values(), key=lambda h: h.value_usd, reverse=True)
    if not holdings:
        raise ValueError(f"No holdings parsed from {etf_id} N-30B-2 {accession} ({as_of})")

    if reported_total is None or reported_total <= 0:
        raise ValueError(
            f"Missing Total Investments reconciliation row in {etf_id} N-30B-2 {accession} ({as_of})"
        )

    sum_val = sum(h.value_usd for h in holdings)
    diff = abs(sum_val - reported_total)
    if diff > max(1.0, len(holdings) * 1.0):
        raise ValueError(
            f"Total Investments reconciliation failed for {etf_id} N-30B-2 {accession} ({as_of}): "
            f"parsed={sum_val:,.2f} vs reported={reported_total:,.2f} (diff={diff:,.2f})"
        )

    return FilingHoldings(
        etf_id=etf_id,
        as_of=as_of,
        source="N-30B-2",
        accession=accession,
        filed=filed,
        total_value_usd=sum_val,
        holdings=holdings,
    )


def fetch_invesco_legacy_filings(
    client: EdgarClient,
    etf_cfg: dict[str, Any],
    min_as_of: str = "2011-01-01",
    max_as_of: str = "2019-08-31",
) -> list[FilingHoldings]:
    """Fetch and parse pre-NPORT Form N-30B-2 annual reports for an Invesco UIT ETF (e.g. QQQ)."""
    etf_id = etf_cfg["etf_id"]
    cik = normalize_cik(etf_cfg["cik"])
    min_date = etf_cfg.get("legacy_min_date") or min_as_of

    all_n30b2 = client.get_all_filings(cik, forms={"N-30B-2", "N-30B-2/A"})
    candidates = [
        f
        for f in all_n30b2
        if f.get("reportDate") and min_date <= f["reportDate"] <= max_as_of
    ]
    candidates.sort(key=lambda f: (f["reportDate"], f["filingDate"], f["accessionNumber"]))

    by_as_of: dict[str, FilingHoldings] = {}
    for meta in candidates:
        as_of = meta["reportDate"]
        accn = meta["accessionNumber"]
        doc = meta["primaryDocument"]
        html_bytes = client.get_filing_document(cik, accn, doc)
        parsed = parse_invesco_n30b2_html(
            html_bytes=html_bytes,
            etf_id=etf_id,
            as_of=as_of,
            accession=accn,
            filed=meta["filingDate"],
        )
        by_as_of[as_of] = parsed

    return [by_as_of[d] for d in sorted(by_as_of)]
