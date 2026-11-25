"""iShares legacy filings (N-Q / N-CSR / N-CSRS) HTML adapter (2011-2019).

Key behaviors and validation rules:
- Matches fund schedules across historical fund name changes (e.g. "iShares High Dividend Equity Fund"
  -> "iShares High Dividend ETF" -> "iShares Core High Dividend ETF").
- Values are reported in whole dollars ($1) and Shares in whole shares.
- Fails fast if parsed common stock holdings do not reconcile with reported Total Common Stocks
  within $1.
- Falls back to the original filing when an /A amendment omits the holdings schedule.
"""

from __future__ import annotations

import html as html_lib
import re
import statistics
from typing import Any

from lxml import html as lhtml

from src.adapters.nport import FilingHoldings, RawHolding
from src.edgar import EdgarClient, normalize_cik

_SPACE_RE = re.compile(r"\s+")
_NUM_RE = re.compile(r"^\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)$")
# Strip trailing footnote markers (e.g. (a), (b), *, ^, or standalone lowercase superscript letters " a", " a,b")
# and trailing ", NVS" / " NVS" (non-voting shares suffix), while preserving "(The)" and "Class A/B"
_FOOTNOTE_SUFFIX_RE = re.compile(
    r"(?:\s*(?:\([a-z0-9,\s]{1,5}\)|[*†‡§^])|\s+[a-z](?:,[a-z])*|,?\s+NVS)+$"
)
_THE_PAREN_RE = re.compile(r"\s*\(\s*The\s*\)\s*$", re.IGNORECASE)
_BLOCK_TAG_RE = re.compile(r"</?(?:p|div|tr|td|th|br|h[1-6]|li|table)\b[^>]*>|&nbsp;|&#160;", re.IGNORECASE)
_ANY_TAG_RE = re.compile(r"<[^>]+>")


def _html_to_plain(raw: str) -> str:
    """Normalize block boundaries to spaces while joining inline kerning tags seamlessly."""
    s = _BLOCK_TAG_RE.sub(" ", raw)
    s = _ANY_TAG_RE.sub("", s)
    return _clean_text(s)


def _clean_text(text: str) -> str:
    cleaned = (
        html_lib.unescape(text)
        .replace("\xa0", " ")
        .replace("\u200b", "")
        .replace("\x92", "'")
        .replace("\x91", "'")
        .replace("\x93", '"')
        .replace("\x94", '"')
        .replace("\x96", "-")
        .replace("\x97", "-")
        .replace("\u2019", "'")
        .replace("\u2018", "'")
    )
    return _SPACE_RE.sub(" ", cleaned).strip()


def _parse_number(cell: str) -> float | None:
    m = _NUM_RE.match(cell.strip())
    if not m:
        return None
    return float(m.group(1).replace(",", ""))


def _clean_security_name(raw_name: str) -> str:
    name = _clean_text(raw_name)
    # Strip footnote markers iteratively without stripping "(The)"
    prev = None
    while prev != name:
        prev = name
        if _THE_PAREN_RE.search(name):
            break
        m = _FOOTNOTE_SUFFIX_RE.search(name)
        if not m:
            break
        # Preserve "(The)" and uppercase share class letters
        matched = m.group(0).strip().lower()
        if matched == "(the)":
            break
        # Do not strip lowercase words like "nv" / "sa" / "co" (only single-letter footnotes, NVS, or parens)
        if re.fullmatch(r"[a-z]{2,}", matched) and matched != "nvs":
            break
        name = name[: m.start()].strip()
    return name


def _build_name_regex(name_patterns: list[str]) -> re.Pattern[str]:
    alts: list[str] = []
    for pat in name_patterns:
        words = pat.split()
        word_re = []
        for w in words:
            if w.lower() == "ishares":
                # Allow ® / &reg; / <sup> tags immediately following iShares
                word_re.append(r"iShares(?:<[^>]+>|&reg;|&#174;|®|\s)*")
            else:
                word_re.append(re.escape(w))
        alts.append(r"(?:<[^>]+>|&nbsp;|&#160;|\s)+".join(word_re))
    return re.compile("|".join(alts), re.IGNORECASE)


def _find_schedule_start(html_text: str, name_patterns: list[str]) -> int:
    """Return the byte offset of the target fund's non-continued Schedule of Investments."""
    pattern = _build_name_regex(name_patterns)
    candidates: list[tuple[int, int]] = []

    for m in pattern.finditer(html_text):
        pre_raw = html_text[max(0, m.start() - 1600) : m.start()]
        pre_plain = _html_to_plain(pre_raw).lower()
        tail_raw = html_text[m.end() : m.end() + 4000]
        tail_plain = _html_to_plain(tail_raw).lower()

        # Heading format: "Schedule of Investments ... iSHARES ® <Fund Name>"
        pre_window = pre_plain[-300:]
        if "schedule of investments" not in pre_window:
            continue
        if "continued" in pre_window[-140:] or "continued" in tail_plain[:120]:
            continue
        if "common stocks" not in tail_plain:
            continue

        # Prefer matches where "Security Shares Value" column header appears shortly after
        prio = 0 if ("security" in tail_plain[:1200] and "shares" in tail_plain[:1200]) else 1
        candidates.append((prio, m.start()))

    if not candidates:
        raise ValueError(f"iShares Schedule of Investments not found for {name_patterns!r}")

    candidates.sort(key=lambda x: (x[0], x[1]))
    return candidates[0][1]


def parse_ishares_legacy_html(
    html_bytes: bytes,
    etf_id: str,
    name_patterns: list[str],
    as_of: str,
    form: str,
    accession: str,
    filed: str,
) -> FilingHoldings:
    """Extract all common stock holdings from iShares legacy HTML and reconcile with Total Common Stocks."""
    html_text = html_bytes.decode("utf-8", errors="replace")
    start_pos = _find_schedule_start(html_text, name_patterns)

    # Locate the first "Total Common Stocks" after start_pos (up to 3.5MB for 400+ holding funds like DGRO)
    search_win = html_text[start_pos : start_pos + 3_500_000]
    total_pat = re.compile(
        r"total(?:<[^>]+>|&nbsp;|&#160;|\s)+common(?:<[^>]+>|&nbsp;|&#160;|\s)+stocks?",
        re.IGNORECASE,
    )
    m_end = total_pat.search(search_win)
    if m_end:
        chunk = html_text[start_pos : min(len(html_text), start_pos + m_end.end() + 8000)]
    else:
        chunk = html_text[start_pos : start_pos + 450_000]
    doc = lhtml.fromstring(chunk)

    in_common_stocks = False
    waiting_total_value = False
    reported_total: float | None = None
    merged: dict[str, RawHolding] = {}

    for tr in doc.xpath(".//tr"):
        raw_cells = [_clean_text(" ".join(td.itertext())) for td in tr.xpath("./td|./th")]
        cells = [c for c in raw_cells if c and c != "$"]
        if not cells:
            continue

        row_text = " ".join(cells)
        row_low = row_text.lower()

        if not in_common_stocks:
            if row_low.startswith("common stocks"):
                in_common_stocks = True
            continue

        # Handle 2-line Total Common Stocks row: next row has "(Cost: $...) | <Value>"
        if waiting_total_value:
            val_num = _parse_number(cells[-1])
            if val_num is not None and val_num > 0:
                reported_total = val_num
                break
            waiting_total_value = False

        if "total common stocks" in row_low:
            val_num = _parse_number(cells[-1])
            if val_num is not None and val_num > 0:
                reported_total = val_num
                break
            waiting_total_value = True
            continue

        # Stop when entering another asset class or summary section
        if row_low.startswith(
            ("short-term investments", "total short-term", "total investments", "money market")
        ):
            break

        # Extract holding row: name cell(s), shares cell, market value cell
        if len(cells) >= 3:
            val_num = _parse_number(cells[-1])
            sh_num = _parse_number(cells[-2])
            if sh_num is not None and val_num is not None and sh_num > 0 and val_num > 0:
                name_part = " ".join(cells[:-2])
                name_low = name_part.lower()
                if name_low in ("security", "shares", "value", "total", "subtotal") or name_low.startswith(
                    ("total ", "subtotal ")
                ):
                    continue
                sec_name = _clean_security_name(name_part)
                if not sec_name:
                    continue

                merge_key = sec_name.upper()
                if merge_key in merged:
                    merged[merge_key].shares += sh_num
                    merged[merge_key].value_usd += val_num
                else:
                    merged[merge_key] = RawHolding(
                        reported_name=sec_name,
                        shares=sh_num,
                        value_usd=val_num,
                        cusip=None,
                        isin=None,
                        pct_net_assets=None,
                    )

    if reported_total is None:
        raise ValueError(
            f"Total Common Stocks row not found in {etf_id} {form} {accession} ({as_of})"
        )

    holdings = sorted(merged.values(), key=lambda h: h.value_usd, reverse=True)
    if not holdings:
        raise ValueError(f"No holdings parsed from {etf_id} {form} {accession} ({as_of})")

    # Sanity-check against unexpected $000 scaling
    sample_prices = [h.value_usd / h.shares for h in holdings if h.shares > 0]
    if sample_prices and statistics.median(sample_prices) < 1.0:
        raise ValueError(
            f"Unexpected low median price (${statistics.median(sample_prices):.4f}) "
            f"in {etf_id} {as_of} ({accession})"
        )

    calc_total = sum(h.value_usd for h in holdings)
    diff = abs(calc_total - reported_total)
    if diff > 1.0:
        raise ValueError(
            f"Total Common Stocks mismatch for {etf_id} {as_of} ({accession}): "
            f"parsed={calc_total:,.2f}, reported={reported_total:,.2f}, diff={diff:,.2f}"
        )

    return FilingHoldings(
        etf_id=etf_id,
        as_of=as_of,
        source=form,
        accession=accession,
        filed=filed,
        total_value_usd=calc_total,
        holdings=holdings,
    )


def fetch_ishares_legacy_filings(
    client: EdgarClient,
    etf_cfg: dict[str, Any],
    min_as_of: str = "2011-01-01",
    max_as_of: str = "2019-09-30",
) -> list[FilingHoldings]:
    """Fetch and parse all iShares legacy filings (N-Q / N-CSR / N-CSRS) for the target ETF."""
    etf_id = etf_cfg["etf_id"]
    cik = normalize_cik(etf_cfg["cik"])
    fy_end_month = int(etf_cfg.get("fiscal_year_end_month") or 4)
    name_patterns: list[str] = list(
        etf_cfg.get("legacy_name_patterns") or [etf_cfg["name"]]
    )

    semiannual_month = ((fy_end_month + 5) % 12) + 1
    q1_month = ((fy_end_month + 2) % 12) + 1
    q3_month = ((fy_end_month + 8) % 12) + 1

    all_legacy = client.get_all_filings(
        cik, forms={"N-CSR", "N-CSR/A", "N-CSRS", "N-CSRS/A", "N-Q", "N-Q/A"}
    )
    by_accn = {
        f["accessionNumber"]: f
        for f in all_legacy
        if f["reportDate"] and min_as_of <= f["reportDate"] <= max_as_of
    }

    by_date: dict[str, list[dict[str, str]]] = {}
    seen_accns: set[str] = set()
    for idx, pat in enumerate(name_patterns):
        cache_key = f"{etf_id}_legacy_name_{idx}"
        hits = client.search_efts_accessions(f'"{pat}"', "N-Q,N-CSR,N-CSRS", cache_key)
        for hit in hits:
            adsh = str((hit.get("_source") or {}).get("adsh") or "")
            if adsh not in by_accn or adsh in seen_accns:
                continue
            meta = by_accn[adsh]
            rd = meta["reportDate"]
            if len(rd) < 7:
                continue
            mm = int(rd[5:7])
            base_form = meta["form"].replace("/A", "")
            matches_cycle = (
                (mm == fy_end_month and base_form == "N-CSR")
                or (mm == semiannual_month and base_form == "N-CSRS")
                or (mm in (q1_month, q3_month) and base_form == "N-Q")
            )
            if not matches_cycle:
                continue
            seen_accns.add(adsh)
            by_date.setdefault(rd, []).append(meta)

    results: list[FilingHoldings] = []
    for rd in sorted(by_date):
        cands = sorted(
            by_date[rd],
            key=lambda m: (m["filingDate"], m["accessionNumber"]),
            reverse=True,
        )
        last_err: Exception | None = None
        parsed: FilingHoldings | None = None
        for meta in cands:
            html_bytes = client.get_filing_document(
                cik, meta["accessionNumber"], meta["primaryDocument"]
            )
            try:
                parsed = parse_ishares_legacy_html(
                    html_bytes=html_bytes,
                    etf_id=etf_id,
                    name_patterns=name_patterns,
                    as_of=rd,
                    form=meta["form"],
                    accession=meta["accessionNumber"],
                    filed=meta["filingDate"],
                )
                break
            except ValueError as exc:
                last_err = exc
                if meta["form"].endswith("/A"):
                    continue
                raise
        if parsed is None:
            raise ValueError(f"Failed to parse {etf_id} {rd}: {last_err}")
        results.append(parsed)

    return results
