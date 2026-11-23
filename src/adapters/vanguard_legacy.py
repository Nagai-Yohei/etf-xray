"""Vanguard legacy filings (N-Q / N-CSR / N-CSRS) HTML adapter (2011-2019).

Key behaviors and validation rules:
- Vanguard legacy schedules (Statement of Net Assets / Schedule of Investments) report
  Market Value in thousands ($000) and Shares in whole shares.
- Exclude non-target funds sharing title words (e.g. International High Dividend Yield Index Fund).
- Fail fast if parsed holdings total does not reconcile with Total Common Stocks within
  $000 rounding tolerance.
"""

from __future__ import annotations

import re
from typing import Any

from lxml import html as lhtml

from src.adapters.nport import FilingHoldings, RawHolding
from src.edgar import EdgarClient, normalize_cik

_SPACE_RE = re.compile(r"\s+")
_NUM_RE = re.compile(r"^\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)$")
# Merged cells (e.g. "General Electric Co. 129,315,198" + "4,086,360" or "Apple Inc." + "88,905,773 16,457,348")
_MERGED_ROW_RE = re.compile(
    r"^(.+?)\s+([0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)\s+\$?\s*([0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)$"
)
# Leading footnote-marker cell (e.g. *, ^, 1, 2, *,^, §,*, 1,2)
_LEAD_MARKER_RE = re.compile(r"^[\*\^†‡§•·,0-9\s]+$")
# Leading symbol markers attached to company name (preserve digits for 3M Co. / 1st Source Corp.)
_LEAD_SYMBOL_RE = re.compile(r"^[\*\^†‡§•·,]+\s*")
# Trailing footnote markers (e.g. " 1", " 1,2", " *", " ^", " (a)" — preserve Class A/B and 3M)
_TRAIL_FOOTNOTE_RE = re.compile(
    r"(?:\s+(?:[1-9](?:,[1-9])*|[\*\^†‡§•·]+|\([a-z0-9,\s]+\))|\s*[\*\^†‡§•·]+)+$"
)
_BLOCK_TAG_RE = re.compile(
    r"</?(?:p|div|tr|td|th|br|h[1-6]|li|table)\b[^>]*>|&nbsp;|&#160;", re.IGNORECASE
)
_ANY_TAG_RE = re.compile(r"<[^>]+>")
# Sector heading row (e.g. "Consumer Discretionary (6.6%)")
_SECTOR_HEADER_RE = re.compile(r"\(\s*\d+(?:\.\d+)?\s*%\s*\)")
# Purely numeric / dash / symbol rows (sector subtotals, page numbers, ($000), etc.)
_PURE_NUM_OR_DASH_RE = re.compile(r"^[\$\d,.\s\-—–()]+$")
# Zero/sub-thousand residual value markers ("-" / "—")
_DASH_VAL_RE = re.compile(r"^[\-—–]+$")
# Summary/subtotal rows (restricted keywords to avoid excluding "Total System Services Inc.")
_SUMMARY_ROW_RE = re.compile(
    r"^(?:total|subtotal)\s+(?:common|stocks?|investments?|equit(?:y|ies)|"
    r"consumer|energy|financials?|health|industrials?|information|materials?|"
    r"real\s+estate|telecommunication|communication|utilit(?:y|ies)|other)"
)
# Disallowed prefix qualifiers for sibling funds (prevents false exclusion from prior page text)
_FORBIDDEN_PREFIX_WORDS = (
    "international",
    "ex-u.s.",
    "ex-us",
    "tax-managed",
    "extended",
    "institutional",
    "mega cap",
    "mid-cap",
    "small-cap",
)


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
        .replace("\u2019", "'")
        .replace("\u2018", "'")
    )
    return _SPACE_RE.sub(" ", cleaned).strip()


def _parse_number(cell: str) -> float | None:
    m = _NUM_RE.match(cell.strip())
    if not m:
        return None
    return float(m.group(1).replace(",", ""))


def _html_to_plain(raw: str) -> str:
    """Strip inline kerning tags (span/font) without adding spaces; normalize block boundaries to spaces."""
    s = _BLOCK_TAG_RE.sub(" ", raw)
    s = _ANY_TAG_RE.sub("", s)
    return _clean_text(s)


def _td_text(td: Any) -> str:
    """Extract table cell text, joining inline tags seamlessly and spacing only block/sup boundaries."""
    parts: list[str] = []

    def _walk(node: Any) -> None:
        tag = node.tag.lower() if isinstance(node.tag, str) else ""
        if tag in ("br", "p", "div", "tr", "li", "sup"):
            parts.append(" ")
        if node.text:
            parts.append(node.text)
        for child in node:
            _walk(child)
            if child.tail:
                parts.append(child.tail)
        if tag in ("p", "div", "tr", "li"):
            parts.append(" ")

    _walk(td)
    return _clean_text("".join(parts))


def _word_html_pat(word: str) -> str:
    """Return a regex matching a word even when inline kerning tags are inserted between characters."""
    return r"(?:<[^>]+>)*".join(re.escape(ch) for ch in word)


def _clean_security_name(raw_name: str) -> str:
    name = _clean_text(raw_name)
    # Strip leading symbol markers (e.g. "* Apple Inc.")
    name = _LEAD_SYMBOL_RE.sub("", name).strip()
    # Repeatedly strip trailing footnote markers
    prev = None
    while prev != name:
        prev = name
        name = _TRAIL_FOOTNOTE_RE.sub("", name).strip()
    return name


def _find_section_bounds(html_text: str, fund_title: str) -> tuple[int, int]:
    """Return (start, end) byte offsets for the fund's Statement of Net Assets / Schedule of Investments."""
    words = fund_title.split()
    name_pat = r"(?:<[^>]+>|&nbsp;|&#160;|\s)+".join(_word_html_pat(w) for w in words)
    pattern = re.compile(name_pat, re.IGNORECASE)

    # Disallowed sibling-fund qualifiers (exclude words present in the target fund title itself)
    title_low = fund_title.lower()
    forbidden = [w for w in _FORBIDDEN_PREFIX_WORDS if w not in title_low]

    candidates: list[tuple[int, int]] = []
    for m in pattern.finditer(html_text):
        # Check preceding 25 chars for sibling-fund prefixes (e.g. International High Dividend...)
        prefix_raw = html_text[max(0, m.start() - 120) : m.start()]
        prefix_plain = _html_to_plain(prefix_raw).lower()
        prefix_tail = prefix_plain[-25:]
        if any(fw in prefix_tail for fw in forbidden):
            continue

        tail_raw = html_text[m.end() : m.end() + 20000]
        tail_plain = _html_to_plain(tail_raw).lower()

        # Skip audit report text
        if "in our opinion" in tail_plain[:160]:
            continue
        # Confirm this is a portfolio holdings schedule heading
        has_stmt = (
            "statement of net assets" in tail_plain[:260]
            or "schedule of investments" in tail_plain[:260]
        )
        if not has_stmt:
            continue
        if "common stocks" not in tail_plain:
            continue

        # Prefer headings containing "As of"
        prio = 0 if "as of" in tail_plain[:350] else 1
        candidates.append((prio, m.start()))

    if not candidates:
        raise ValueError(f"Vanguard holdings section not found for {fund_title!r}")

    candidates.sort(key=lambda x: (x[0], x[1]))
    start_pos = candidates[0][1]

    # Locate the first "Total Common Stocks" after start_pos (up to 4.5MB window)
    search_window = html_text[start_pos : start_pos + 4_500_000]
    total_pat = re.compile(
        r"(?:<[^>]+>|&nbsp;|&#160;|\s)+".join(
            _word_html_pat(w) for w in ("Total", "Common", "Stocks")
        ),
        re.IGNORECASE,
    )
    m_end = total_pat.search(search_window)
    if not m_end:
        raise ValueError(f"'Total Common Stocks' not found after section start for {fund_title!r}")

    end_pos = start_pos + m_end.end() + 8000
    return start_pos, min(len(html_text), end_pos)


def parse_vanguard_legacy_html(
    html_bytes: bytes,
    etf_id: str,
    fund_title: str,
    as_of: str,
    form: str,
    accession: str,
    filed: str,
) -> FilingHoldings:
    """Extract all common stock holdings from Vanguard legacy HTML and reconcile with Total Common Stocks."""
    html_text = html_bytes.decode("utf-8", errors="replace")
    start_pos, end_pos = _find_section_bounds(html_text, fund_title)
    chunk = html_text[start_pos:end_pos]

    # Auto-detect thousands ($000) scaling
    header_plain = _html_to_plain(chunk[:12000]).lower()
    value_multiplier = 1000.0 if ("$000" in header_plain or "($000" in header_plain or "in thousands" in header_plain) else 1.0

    doc = lhtml.fromstring(chunk)

    fund_title_low = fund_title.lower()
    in_common_stocks = False
    waiting_total_val = False
    reported_total_raw: float | None = None
    pending_name_parts: list[str] = []
    merged: dict[str, RawHolding] = {}

    def _join_name_parts(parts: list[str]) -> str:
        out = ""
        for part in parts:
            p = part.strip()
            if not p:
                continue
            if not out:
                out = p
            elif out.endswith("-"):
                out = out + p
            else:
                out = out + " " + p
        return out

    for tr in doc.xpath(".//tr"):
        raw_cells = [_td_text(td) for td in tr.xpath("./td|./th")]
        cells = [c for c in raw_cells if c and c != "$"]
        if not cells:
            continue

        row_text = " ".join(cells)
        row_low = row_text.lower()

        if not in_common_stocks:
            if row_low.startswith("common stocks"):
                in_common_stocks = True
            continue

        # Value row for 2-line Total Common Stocks
        if waiting_total_val:
            num = _parse_number(cells[-1])
            if num is not None and num > 0:
                reported_total_raw = num
                break
            waiting_total_val = False

        if "total common stocks" in row_low:
            pending_name_parts.clear()
            num = _parse_number(cells[-1])
            if num is not None and num > 0:
                reported_total_raw = num
                break
            waiting_total_val = True
            continue

        # Safety guard for end of common stocks section
        if row_low.startswith(
            ("temporary cash investments", "preferred stocks", "corporate bonds", "total investments")
        ):
            break

        # Extract holding row (normally >= 3 cells, or 2 cells with merged Name+Shares / Shares+Value)
        data_cells = list(cells)
        while len(data_cells) >= 2 and _LEAD_MARKER_RE.match(data_cells[0]):
            data_cells = data_cells[1:]

        line_name: str | None = None
        sh_num: float | None = None
        val_num: float | None = None

        if len(data_cells) >= 3:
            c_val = _parse_number(data_cells[-1])
            c_sh = _parse_number(data_cells[-2])
            # Skip sub-$1k residual positions ("-" / "—") after clearing pending wrapped name parts
            if c_sh is not None and _DASH_VAL_RE.match(data_cells[-1].strip()):
                pending_name_parts.clear()
                continue
            if c_sh is not None and c_val is not None and c_sh > 0 and c_val > 0:
                line_name = " ".join(data_cells[:-2])
                sh_num = c_sh
                val_num = c_val

        # Recover rows where PDF-to-HTML conversion merged Name+Shares or Shares+Value into one cell
        if line_name is None and len(data_cells) >= 2 and not _SECTOR_HEADER_RE.search(row_text):
            joined = " ".join(data_cells)
            m_merged = _MERGED_ROW_RE.match(joined)
            if m_merged:
                cand_name = m_merged.group(1).strip()
                c_sh = _parse_number(m_merged.group(2))
                c_val = _parse_number(m_merged.group(3))
                if (
                    cand_name
                    and not _LEAD_MARKER_RE.match(cand_name)
                    and c_sh is not None
                    and c_val is not None
                    and c_sh > 0
                    and c_val > 0
                ):
                    line_name = cand_name
                    sh_num = c_sh
                    val_num = c_val

        if line_name is not None and sh_num is not None and val_num is not None:
            full_name_raw = _join_name_parts([*pending_name_parts, line_name])
            pending_name_parts.clear()
            name_low = full_name_raw.lower()
            if (
                name_low in ("shares", "market value", "value", "market", "total")
                or _SUMMARY_ROW_RE.match(name_low)
            ):
                continue
            sec_name = _clean_security_name(full_name_raw)
            if not sec_name or len(sec_name) < 2:
                continue

            val_usd = val_num * value_multiplier
            merge_key = sec_name.upper()
            if merge_key in merged:
                merged[merge_key].shares += sh_num
                merged[merge_key].value_usd += val_usd
            else:
                merged[merge_key] = RawHolding(
                    reported_name=sec_name,
                    shares=sh_num,
                    value_usd=val_usd,
                    cusip=None,
                    isin=None,
                    pct_net_assets=None,
                )
            continue

        # Non-data rows: reset pending wrapped name on sector headings, subtotals, or page/column headers
        if (
            _SECTOR_HEADER_RE.search(row_text)
            or _PURE_NUM_OR_DASH_RE.match(row_text)
            or fund_title_low in row_low
            or row_low
            in (
                "market",
                "value",
                "shares",
                "($000)",
                "shares ($000)",
                "market value",
                "market value ($000)",
                "security",
                "description",
                "continued",
            )
            or ("market" in row_low and "value" in row_low)
            or ("shares" in row_low and "000" in row_low)
            or row_low.startswith(
                (
                    "statement of net assets",
                    "schedule of investments",
                    "common stocks",
                    "as of ",
                    "see accompanying",
                    "vanguard whitehall",
                    "vanguard index funds",
                )
            )
        ):
            pending_name_parts.clear()
            continue

        # Leading wrapped name line (narrow ~2011 layouts split security names across 2-3 lines)
        wrap_cells = list(cells)
        while len(wrap_cells) >= 2 and _LEAD_MARKER_RE.match(wrap_cells[0]):
            wrap_cells = wrap_cells[1:]
        wrap_text = " ".join(wrap_cells).strip()
        if wrap_text and not _LEAD_MARKER_RE.match(wrap_text):
            pending_name_parts.append(wrap_text)

    if reported_total_raw is None:
        raise ValueError(
            f"Total Common Stocks value not found in {etf_id} {form} {accession} ({as_of})"
        )

    holdings = sorted(merged.values(), key=lambda h: h.value_usd, reverse=True)
    if not holdings:
        raise ValueError(f"No holdings parsed from {etf_id} {form} {accession} ({as_of})")

    reported_total_usd = reported_total_raw * value_multiplier
    calc_total_usd = sum(h.value_usd for h in holdings)
    diff_usd = abs(calc_total_usd - reported_total_usd)

    # Known typesetting omission in source SEC filing (2011-06-30 VOO N-CSRS 0000932471-11-003264
    # omits 1 row for salesforce.com Inc. ~$161,114k at an Information Technology page break)
    _KNOWN_FILING_ERRATA_REL_TOL = {
        "0000932471-11-003264": 0.002,
    }

    # Reconcile total within $000 rounding tolerance (<= $0.5k per holding) and <= 0.1% relative error
    if value_multiplier == 1000.0:
        max_tol_usd = max(50_000.0, len(holdings) * 1000.0)
        max_rel_err = _KNOWN_FILING_ERRATA_REL_TOL.get(accession, 0.001)
        rel_err = diff_usd / reported_total_usd if reported_total_usd > 0 else 1.0
        if diff_usd > max_tol_usd and rel_err > max_rel_err:
            raise ValueError(
                f"Total Common Stocks mismatch for {etf_id} {as_of} ({accession}): "
                f"parsed=${calc_total_usd:,.0f}, reported=${reported_total_usd:,.0f}, "
                f"diff=${diff_usd:,.0f} ({rel_err*100:.3f}%)"
            )
    elif diff_usd > 1.0:
        raise ValueError(
            f"Total Common Stocks mismatch for {etf_id} {as_of} ({accession}): "
            f"parsed=${calc_total_usd:,.2f}, reported=${reported_total_usd:,.2f}"
        )

    return FilingHoldings(
        etf_id=etf_id,
        as_of=as_of,
        source=form,
        accession=accession,
        filed=filed,
        total_value_usd=calc_total_usd,
        holdings=holdings,
    )


def fetch_vanguard_legacy_filings(
    client: EdgarClient,
    etf_cfg: dict[str, Any],
    min_as_of: str = "2011-01-01",
    max_as_of: str = "2019-08-31",
) -> list[FilingHoldings]:
    """Fetch and parse all Vanguard legacy filings (N-Q / N-CSR / N-CSRS)."""
    etf_id = etf_cfg["etf_id"]
    cik = normalize_cik(etf_cfg["cik"])
    fund_title = etf_cfg.get("legacy_fund_title") or etf_cfg["name"]

    all_legacy = client.get_all_filings(
        cik, forms={"N-CSR", "N-CSR/A", "N-CSRS", "N-CSRS/A", "N-Q", "N-Q/A"}
    )

    by_date: dict[str, list[dict[str, str]]] = {}
    for meta in all_legacy:
        rd = meta["reportDate"]
        if not rd or not (min_as_of <= rd <= max_as_of):
            continue
        by_date.setdefault(rd, []).append(meta)

    results: list[FilingHoldings] = []
    for rd in sorted(by_date):
        # Prefer newer /A amendments, falling back to the original filing if /A is certification-only
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
                parsed = parse_vanguard_legacy_html(
                    html_bytes=html_bytes,
                    etf_id=etf_id,
                    fund_title=fund_title,
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
