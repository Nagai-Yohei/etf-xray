"""Shared N-PORT-P (XML) adapter (2019-present).

AGENTS.md rules:
- Extract common equity long positions only (assetCat == "EC" and payoffProfile == "Long"),
  excluding MMFs, futures, and securities-lending collateral.
- Fall back to ISIN for foreign issuers where CUSIP is "000000000".
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from lxml import etree

from src.edgar import EdgarClient, normalize_cik

# Non-common-equity instrument markers in N-PORT <title> (CVRs, rights, escrow receipts, warrants)
_NON_COMMON_TITLE_RE = re.compile(
    r"\b(?:CVR|CONTINGENT\s+VALUE|ESCROW|WARRANTS?|RIGHTS?)\b",
    re.IGNORECASE,
)


@dataclass
class RawHolding:
    """Raw single-security record extracted from a filing by an adapter."""

    reported_name: str
    shares: float
    value_usd: float
    cusip: str | None = None
    isin: str | None = None
    pct_net_assets: float | None = None


@dataclass
class FilingHoldings:
    """Complete set of holdings for a single reporting date."""

    etf_id: str
    as_of: str
    source: str
    accession: str
    filed: str
    total_value_usd: float
    holdings: list[RawHolding]


def _local_name(tag: Any) -> str:
    if not isinstance(tag, str):
        return ""
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


def _find_child_text(elem: etree._Element, local_tag: str) -> str:
    for child in elem:
        if _local_name(child.tag) == local_tag:
            return (child.text or "").strip()
    return ""


def _find_child(elem: etree._Element, local_tag: str) -> etree._Element | None:
    for child in elem:
        if _local_name(child.tag) == local_tag:
            return child
    return None


def parse_nport_xml(
    xml_bytes: bytes,
    etf_id: str,
    expected_series_id: str,
    accession: str,
    filed: str,
) -> FilingHoldings:
    """Parse N-PORT-P primary_doc.xml and return common equity long holdings."""
    parser = etree.XMLParser(recover=False, resolve_entities=False, no_network=True)
    root = etree.fromstring(xml_bytes, parser=parser)

    form_data = _find_child(root, "formData")
    if form_data is None:
        raise ValueError(f"N-PORT-P XML missing formData: {accession}")

    gen_info = _find_child(form_data, "genInfo")
    if gen_info is None:
        raise ValueError(f"N-PORT-P XML missing genInfo: {accession}")

    series_id = _find_child_text(gen_info, "seriesId")
    if expected_series_id and series_id != expected_series_id:
        raise ValueError(
            f"Series ID mismatch in {accession}: expected {expected_series_id}, got {series_id}"
        )

    as_of = _find_child_text(gen_info, "repPdDate")[:10]
    if len(as_of) != 10:
        raise ValueError(f"Invalid repPdDate in {accession}: {as_of!r}")

    invst_or_secs = _find_child(form_data, "invstOrSecs")
    if invst_or_secs is None:
        raise ValueError(f"N-PORT-P XML missing invstOrSecs: {accession}")

    # Aggregate multiple lots sharing the same security identifier
    merged: dict[str, RawHolding] = {}
    for item in invst_or_secs:
        if _local_name(item.tag) != "invstOrSec":
            continue

        asset_cat = _find_child_text(item, "assetCat").upper()
        payoff = _find_child_text(item, "payoffProfile")
        if asset_cat != "EC" or payoff != "Long":
            continue

        name = _find_child_text(item, "name")
        title = _find_child_text(item, "title")
        if not name:
            name = title

        # Exclude CVRs, contingent value rights, escrow receipts, rights, and warrants
        # even if N-PORT categorizes them as assetCat="EC" (preserve issuers like "CVR Energy Inc")
        if title and _NON_COMMON_TITLE_RE.search(title):
            if "CVR ENERGY" not in name.upper():
                continue

        cusip_raw = _find_child_text(item, "cusip").strip().upper()
        cusip = (
            cusip_raw
            if (len(cusip_raw) == 9 and cusip_raw.isalnum() and cusip_raw != "000000000")
            else None
        )

        isin: str | None = None
        identifiers = _find_child(item, "identifiers")
        if identifiers is not None:
            for id_node in identifiers:
                if _local_name(id_node.tag) == "isin":
                    val = (id_node.get("value") or "").strip().upper()
                    if len(val) == 12 and val.isalnum() and val != "000000000000":
                        isin = val
                        break

        balance_str = _find_child_text(item, "balance")
        val_usd_str = _find_child_text(item, "valUSD")
        pct_val_str = _find_child_text(item, "pctVal")
        if not balance_str or not val_usd_str:
            continue

        shares = float(balance_str)
        value_usd = float(val_usd_str)
        if shares <= 0 or value_usd <= 0:
            continue

        # Normalize N-PORT pctVal from percentage (e.g. 4.12 = 4.12%) to decimal ratio (0.0412)
        pct_net = (float(pct_val_str) / 100.0) if pct_val_str else None

        merge_key = cusip or isin or f"NAME:{name.upper()}"
        if merge_key in merged:
            prev = merged[merge_key]
            prev.shares += shares
            prev.value_usd += value_usd
            if prev.pct_net_assets is not None and pct_net is not None:
                prev.pct_net_assets += pct_net
            if not prev.isin and isin:
                prev.isin = isin
        else:
            merged[merge_key] = RawHolding(
                reported_name=name,
                shares=shares,
                value_usd=value_usd,
                cusip=cusip,
                isin=isin,
                pct_net_assets=pct_net,
            )

    holdings = sorted(merged.values(), key=lambda h: h.value_usd, reverse=True)
    if not holdings:
        raise ValueError(f"No EC/Long holdings extracted from {accession} ({as_of})")

    total_value = sum(h.value_usd for h in holdings)
    return FilingHoldings(
        etf_id=etf_id,
        as_of=as_of,
        source="NPORT-P",
        accession=accession,
        filed=filed,
        total_value_usd=total_value,
        holdings=holdings,
    )


def _discover_nport_candidates(
    client: EdgarClient,
    etf_cfg: dict[str, Any],
    *,
    max_age_days: float | None = None,
    min_filing_date: str | None = None,
    offline_only: bool = False,
) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]]]:
    """Return (all_nport_by_accn, candidate_accns) for the target ETF."""
    etf_id = etf_cfg["etf_id"]
    cik = normalize_cik(etf_cfg["cik"])
    series_id = etf_cfg["series_id"]
    patterns = etf_cfg.get("series_name_patterns") or [etf_cfg["name"]]

    all_nport = client.get_all_filings(
        cik,
        forms={"NPORT-P", "NPORT-P/A"},
        max_age_days=max_age_days,
        min_filing_date=min_filing_date,
        offline_only=offline_only,
    )
    by_accn = {f["accessionNumber"]: f for f in all_nport}

    # Single-fund UITs (e.g. DIA, QQQ) have no series_id; every NPORT-P under the CIK belongs to the ETF
    if series_id is None:
        return by_accn, dict(by_accn)

    # Filter candidate filings whose primary_doc.xml contains the target fund name / series_id via EFTS
    candidate_accns: dict[str, dict[str, str]] = {}
    for idx, pat in enumerate(patterns):
        cache_key = f"{etf_id}_nport_name" if idx == 0 else f"{etf_id}_nport_name_{idx}"
        hits = client.search_efts_accessions(
            f'"{pat}"',
            "NPORT-P",
            cache_key,
            max_age_days=max_age_days,
            offline_only=offline_only,
        )
        for hit in hits:
            hid = str(hit.get("_id") or "")
            adsh = str((hit.get("_source") or {}).get("adsh") or "")
            if adsh in by_accn and hid.endswith("primary_doc.xml"):
                candidate_accns[adsh] = by_accn[adsh]

    return by_accn, candidate_accns


def fetch_etf_nport_filings(
    client: EdgarClient,
    etf_cfg: dict[str, Any],
) -> list[FilingHoldings]:
    """Fetch and parse all N-PORT-P filings for the target ETF, sorted by reporting date."""
    etf_id = etf_cfg["etf_id"]
    cik = normalize_cik(etf_cfg["cik"])
    series_id = etf_cfg["series_id"]

    _, candidate_accns = _discover_nport_candidates(client, etf_cfg)

    # Prefer the latest filingDate when amendments (NPORT-P/A) exist for the same reportDate
    by_as_of: dict[str, FilingHoldings] = {}
    for accn, meta in sorted(
        candidate_accns.items(),
        key=lambda kv: (kv[1]["reportDate"], kv[1]["filingDate"], kv[0]),
    ):
        doc_name = meta.get("primaryDocument") or "primary_doc.xml"
        xml_bytes = client.get_filing_document(cik, accn, doc_name)
        try:
            parsed = parse_nport_xml(
                xml_bytes=xml_bytes,
                etf_id=etf_id,
                expected_series_id=series_id,
                accession=accn,
                filed=meta["filingDate"],
            )
        except ValueError as exc:
            # Skip unrelated series whose names matched incidentally; re-raise all other errors
            if "Series ID mismatch" in str(exc):
                continue
            raise

        prev = by_as_of.get(parsed.as_of)
        if prev is None or parsed.filed >= prev.filed:
            by_as_of[parsed.as_of] = parsed

    return [by_as_of[d] for d in sorted(by_as_of)]


def fetch_new_etf_nport_filings(
    client: EdgarClient,
    etf_cfg: dict[str, Any],
    existing_dates: dict[str, str],
    *,
    max_age_days: float | None = 0.5,
    offline_only: bool = False,
) -> list[FilingHoldings]:
    """Fetch and parse only new or newly amended N-PORT-P filings not yet in existing_dates.

    `existing_dates` maps reporting date (`as_of`) -> stored `accession`.
    """
    etf_id = etf_cfg["etf_id"]
    cik = normalize_cik(etf_cfg["cik"])
    series_id = etf_cfg["series_id"]

    min_filing_date: str | None = None
    if existing_dates:
        latest_as_of = max(existing_dates)
        # Keep a 1-year lookback window to catch late NPORT-P/A amendments
        if len(latest_as_of) >= 4 and latest_as_of[:4].isdigit():
            min_filing_date = f"{int(latest_as_of[:4]) - 1}{latest_as_of[4:]}"

    by_accn, candidate_accns = _discover_nport_candidates(
        client,
        etf_cfg,
        max_age_days=max_age_days,
        min_filing_date=min_filing_date,
        offline_only=offline_only,
    )

    known_accessions = {accn for accn in existing_dates.values() if accn}
    by_as_of: dict[str, FilingHoldings] = {}

    for accn, meta in sorted(
        candidate_accns.items(),
        key=lambda kv: (kv[1]["reportDate"], kv[1]["filingDate"], kv[0]),
    ):
        if accn in known_accessions:
            continue

        report_date = meta.get("reportDate") or ""
        filing_date = meta.get("filingDate") or ""
        existing_accn = existing_dates.get(report_date)
        if existing_accn:
            existing_meta = by_accn.get(existing_accn)
            # Skip superseded originals when a newer amendment is already stored
            if existing_meta and filing_date <= (existing_meta.get("filingDate") or ""):
                continue

        doc_name = meta.get("primaryDocument") or "primary_doc.xml"
        xml_bytes = client.get_filing_document(
            cik, accn, doc_name, offline_only=offline_only
        )
        try:
            parsed = parse_nport_xml(
                xml_bytes=xml_bytes,
                etf_id=etf_id,
                expected_series_id=series_id,
                accession=accn,
                filed=filing_date,
            )
        except ValueError as exc:
            if "Series ID mismatch" in str(exc):
                continue
            raise

        # Re-check against actual XML repPdDate in case submission metadata reportDate differed
        stored_accn = existing_dates.get(parsed.as_of)
        if stored_accn == parsed.accession:
            continue
        if stored_accn:
            stored_meta = by_accn.get(stored_accn)
            if stored_meta and parsed.filed <= (stored_meta.get("filingDate") or ""):
                continue

        prev = by_as_of.get(parsed.as_of)
        if prev is None or parsed.filed >= prev.filed:
            by_as_of[parsed.as_of] = parsed

    return [by_as_of[d] for d in sorted(by_as_of)]
