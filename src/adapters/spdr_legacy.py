"""SPDR legacy filings HTML adapters:
- SPDR UIT Form N-30D (e.g. DIA Apr 2011 - Apr 2019)
- SPDR Series Trust multi-fund Form N-CSR / N-CSRS / N-Q (e.g. SPYD Dec 2015 - Jun 2019)

Extracts the full Common Stocks Schedule of Investments table and reconciles every filing
strictly against its reported "Total Common Stocks" line (tolerance <= $1 per stock).
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
_REIT_SUFFIX_RE = re.compile(r"\s+REIT$", re.IGNORECASE)
_TRAILING_COMMA_RE = re.compile(r",+$")
_SECTOR_PCT_RE = re.compile(r"\d+(?:\.\d+)?\s*%")


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
        name = _REIT_SUFFIX_RE.sub("", name).strip()
        name = _TRAILING_COMMA_RE.sub("", name).strip()
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


def _title_to_html_regex(title: str) -> re.Pattern[str]:
    """Build a regex matching a fund title across HTML entities (&amp;, &nbsp;) and whitespace."""
    parts: list[str] = []
    for token in title.split():
        escaped = re.escape(token).replace(r"\&", r"(?:&amp;|&)")
        parts.append(escaped)
    return re.compile(r"(?:<[^>]+>|&nbsp;|&#160;|\s)+".join(parts), re.IGNORECASE)


def parse_spdr_series_html(
    html_bytes: bytes,
    etf_id: str,
    fund_title: str,
    as_of: str,
    form: str,
    accession: str,
    filed: str,
) -> FilingHoldings:
    """Parse a target fund's Schedule of Investments from a multi-fund SPDR Series Trust HTML filing."""
    html_text = html_bytes.decode("utf-8", errors="replace")
    title_pat = _title_to_html_regex(fund_title)

    start_pos: int | None = None
    for m in title_pat.finditer(html_text):
        tail_raw = html_text[m.end() : m.end() + 800]
        tail_plain = _clean_text(re.sub(r"<[^>]+>", " ", tail_raw.replace("&nbsp;", " "))).lower()
        if "schedule of investments" in tail_plain:
            start_pos = m.start()
            break

    if start_pos is None:
        raise ValueError(
            f"SPDR Series Schedule of Investments not found for {fund_title!r} in {etf_id} {form} {accession} ({as_of})"
        )

    search_win = html_text[start_pos : start_pos + 1_500_000]
    total_pat = re.compile(
        r"total(?:<[^>]+>|&nbsp;|&#160;|\s)+common(?:<[^>]+>|&nbsp;|&#160;|\s)+stocks?",
        re.IGNORECASE,
    )
    m_end = total_pat.search(search_win)
    if not m_end:
        raise ValueError(
            f"Missing Total Common Stocks in {etf_id} {form} {accession} ({as_of})"
        )

    chunk = html_text[start_pos : min(len(html_text), start_pos + m_end.end() + 4000)]
    tree = lhtml.fromstring(chunk)

    merged: dict[str, RawHolding] = {}
    reported_total: float | None = None
    in_common_stocks = False
    pending_name_parts: list[str] = []

    for tr in tree.xpath(".//tr"):
        raw_cells = [_clean_text(td.text_content()) for td in tr.xpath("./td|./th")]
        cells = [c for c in raw_cells if c and c != "$"]
        if not cells:
            continue

        first_low = cells[0].lower()

        if not in_common_stocks:
            if first_low.startswith("common stock"):
                in_common_stocks = True
            continue

        if "total common stock" in first_low:
            pending_name_parts.clear()
            for c in reversed(cells[1:]):
                num = _parse_number(c)
                if num is not None and num > 0:
                    reported_total = num
                    break
            break

        if first_low.startswith(("short-term", "preferred stock", "corporate bond", "total investment")):
            pending_name_parts.clear()
            break

        row_text = " ".join(cells)
        if (
            first_low.startswith(("security description", "common stock", "see accompanying", "spdr"))
            or _SECTOR_PCT_RE.search(row_text)
            or "(continued)" in row_text.lower()
            or (len(cells) == 1 and _parse_number(cells[0]) is not None)
        ):
            pending_name_parts.clear()
            continue

        if len(cells) < 3:
            # Capture wrapped company name prefix lines (e.g. "Apartment Investment & Management")
            if len(cells) == 1 and _parse_number(cells[0]) is None:
                pending_name_parts.append(cells[0])
            else:
                pending_name_parts.clear()
            continue

        shares_val = _parse_number(cells[-2])
        value_usd = _parse_number(cells[-1])
        if shares_val is None or value_usd is None or shares_val <= 0 or value_usd <= 0:
            pending_name_parts.clear()
            continue

        full_raw_name = " ".join([*pending_name_parts, *cells[:-2]])
        pending_name_parts.clear()
        name = _clean_security_name(full_raw_name)
        if not name or name.lower().startswith(("total ", "subtotal ", "cost ")):
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
        raise ValueError(f"No holdings parsed from {etf_id} {form} {accession} ({as_of})")

    if reported_total is None or reported_total <= 0:
        raise ValueError(
            f"Missing Total Common Stocks reconciliation value in {etf_id} {form} {accession} ({as_of})"
        )

    sum_val = sum(h.value_usd for h in holdings)
    diff = abs(sum_val - reported_total)
    if diff > max(1.0, len(holdings) * 1.0):
        raise ValueError(
            f"Total Common Stocks reconciliation failed for {etf_id} {form} {accession} ({as_of}): "
            f"parsed={sum_val:,.2f} vs reported={reported_total:,.2f} (diff={diff:,.2f})"
        )

    return FilingHoldings(
        etf_id=etf_id,
        as_of=as_of,
        source=form,
        accession=accession,
        filed=filed,
        total_value_usd=sum_val,
        holdings=holdings,
    )


def fetch_spdr_series_legacy_filings(
    client: EdgarClient,
    etf_cfg: dict[str, Any],
    min_as_of: str = "2011-01-01",
    max_as_of: str = "2019-08-31",
) -> list[FilingHoldings]:
    """Fetch and parse pre-NPORT Form N-CSR/N-CSRS/N-Q filings for an SPDR Series Trust ETF."""
    etf_id = etf_cfg["etf_id"]
    cik = normalize_cik(etf_cfg["cik"])
    fund_title = etf_cfg.get("legacy_fund_title") or etf_cfg["name"]
    min_date = etf_cfg.get("legacy_min_date") or min_as_of

    all_legacy = {
        f["accessionNumber"]: f
        for f in client.get_all_filings(
            cik, forms={"N-CSR", "N-CSR/A", "N-CSRS", "N-CSRS/A", "N-Q", "N-Q/A"}
        )
    }

    hits = client.search_efts_accessions(
        f'"{fund_title}"', "N-CSR,N-CSRS,N-Q", f"{etf_id}_legacy_name"
    )
    candidates: dict[str, dict[str, str]] = {}
    for h in hits:
        adsh = str((h.get("_source") or {}).get("adsh") or "")
        if adsh in all_legacy:
            meta = all_legacy[adsh]
            rd = meta.get("reportDate") or ""
            if rd and min_date <= rd <= max_as_of:
                candidates[adsh] = meta

    by_as_of: dict[str, FilingHoldings] = {}
    for meta in sorted(
        candidates.values(),
        key=lambda f: (f["reportDate"], f["filingDate"], f["accessionNumber"]),
    ):
        as_of = meta["reportDate"]
        accn = meta["accessionNumber"]
        doc = meta.get("primaryDocument")
        if not doc:
            continue
        html_bytes = client.get_filing_document(cik, accn, doc)
        parsed = parse_spdr_series_html(
            html_bytes=html_bytes,
            etf_id=etf_id,
            fund_title=fund_title,
            as_of=as_of,
            form=meta["form"],
            accession=accn,
            filed=meta["filingDate"],
        )
        prev = by_as_of.get(as_of)
        if prev is None or parsed.filed >= prev.filed:
            by_as_of[as_of] = parsed

    return [by_as_of[d] for d in sorted(by_as_of)]
