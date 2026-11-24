"""SPDR UIT legacy filings (Form N-30D) HTML adapter (e.g. DIA Apr 2011 - Apr 2019).

Extracts the full Common Stocks Schedule of Investments table from single-fund UIT
semi-annual / annual N-30D shareholder reports and reconciles every filing against
its reported "Total Common Stocks" line (tolerance <= $1).
"""

from __future__ import annotations

import re
from typing import Any

from lxml import html as lhtml

from src.adapters.nport import FilingHoldings, RawHolding
from src.edgar import EdgarClient, normalize_cik

_SPACE_RE = re.compile(r"\s+")
_NUM_RE = re.compile(r"^\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)$")
# Strip trailing footnote markers (e.g. (a), (b), *, †, ‡) while preserving share classes like Class A
_FOOTNOTE_SUFFIX_RE = re.compile(r"(?:\s*(?:\([a-z0-9,\s]+\)|[*†‡§^]))+$")
_PAREN_CLASS_RE = re.compile(r"\(\s*(Class\s+[A-Z])\s*\)", re.IGNORECASE)


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


def parse_spdr_n30d_html(
    html_bytes: bytes,
    etf_id: str,
    as_of: str,
    accession: str,
    filed: str,
) -> FilingHoldings:
    """Parse a SPDR UIT N-30D Schedule of Investments HTML table with Total Common Stocks reconciliation."""
    tree = lhtml.fromstring(html_bytes)

    target_table = None
    for tbl in tree.xpath("//table"):
        txt = _clean_text(tbl.text_content())
        low = txt.lower()
        if "common stock" in low and "total common stock" in low and "shares" in low and "value" in low:
            target_table = tbl
            break

    if target_table is None:
        raise ValueError(f"Could not find Common Stocks table in {etf_id} N-30D {accession} ({as_of})")

    holdings: list[RawHolding] = []
    reported_total: float | None = None
    in_common_stocks = False

    for tr in target_table.xpath(".//tr"):
        raw_cells = [_clean_text(td.text_content()) for td in tr.xpath("./td|./th")]
        cells = [c for c in raw_cells if c and c != "$"]
        if not cells:
            continue

        first_low = cells[0].lower()

        if "total common stock" in first_low:
            # Extract rightmost numeric value as Total Common Stocks
            for c in reversed(cells[1:]):
                num = _parse_number(c)
                if num is not None and num > 0:
                    reported_total = num
                    break
            break

        if first_low.startswith("common stock"):
            in_common_stocks = True
            continue

        if first_low.startswith("security description") or first_low in ("shares", "value"):
            continue

        if not in_common_stocks:
            continue

        # Standard 3-column holding row: [Security Name, Shares, Value]
        if len(cells) < 3:
            continue
        shares_val = _parse_number(cells[-2])
        value_usd = _parse_number(cells[-1])
        if shares_val is None or value_usd is None or shares_val <= 0 or value_usd <= 0:
            continue

        name = _clean_security_name(" ".join(cells[:-2]))
        if not name or name.lower().startswith(("total", "cost")):
            continue

        holdings.append(
            RawHolding(
                reported_name=name,
                cusip=None,
                isin=None,
                shares=shares_val,
                value_usd=value_usd,
                pct_net_assets=None,
            )
        )

    if not holdings:
        raise ValueError(f"No holdings parsed from {etf_id} N-30D {accession} ({as_of})")

    if reported_total is None or reported_total <= 0:
        raise ValueError(f"Missing Total Common Stocks reconciliation row in {etf_id} N-30D {accession} ({as_of})")

    sum_val = sum(h.value_usd for h in holdings)
    diff = abs(sum_val - reported_total)
    # Require strict reconciliation against Total Common Stocks (<= $1 per stock rounding tolerance)
    if diff > max(1.0, len(holdings) * 1.0):
        raise ValueError(
            f"Total Common Stocks reconciliation failed for {etf_id} N-30D {accession} ({as_of}): "
            f"parsed={sum_val:,.2f} vs reported={reported_total:,.2f} (diff={diff:,.2f})"
        )

    holdings.sort(key=lambda h: h.value_usd, reverse=True)
    return FilingHoldings(
        etf_id=etf_id,
        as_of=as_of,
        source="N-30D",
        accession=accession,
        filed=filed,
        total_value_usd=sum_val,
        holdings=holdings,
    )


def fetch_spdr_legacy_filings(
    client: EdgarClient,
    etf_cfg: dict[str, Any],
    min_as_of: str = "2011-01-01",
    max_as_of: str = "2019-09-30",
) -> list[FilingHoldings]:
    """Fetch and parse pre-NPORT Form N-30D filings for a single-fund SPDR UIT ETF."""
    etf_id = etf_cfg["etf_id"]
    cik = normalize_cik(etf_cfg["cik"])
    min_date = etf_cfg.get("legacy_min_date") or min_as_of

    all_n30d = client.get_all_filings(cik, forms={"N-30D", "N-30D/A"})
    candidates = [
        f
        for f in all_n30d
        if f.get("reportDate") and min_date <= f["reportDate"] <= max_as_of
    ]
    candidates.sort(key=lambda f: (f["reportDate"], f["filingDate"], f["accessionNumber"]))

    by_as_of: dict[str, FilingHoldings] = {}
    for meta in candidates:
        as_of = meta["reportDate"]
        accn = meta["accessionNumber"]
        doc = meta.get("primaryDocument")
        if not doc:
            continue
        html_bytes = client.get_filing_document(cik, accn, doc)
        parsed = parse_spdr_n30d_html(
            html_bytes=html_bytes,
            etf_id=etf_id,
            as_of=as_of,
            accession=accn,
            filed=meta["filingDate"],
        )
        prev = by_as_of.get(as_of)
        if prev is None or parsed.filed >= prev.filed:
            by_as_of[as_of] = parsed

    return [by_as_of[d] for d in sorted(by_as_of)]
