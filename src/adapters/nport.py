"""共通 N-PORT-P（XML）アダプタ（2019年〜）。

AGENTS.md ルール:
- assetCat == "EC" かつ payoffProfile == "Long" のみ普通株として抽出（MMF・先物・貸株担保は除外）。
- 外国籍銘柄は CUSIP が "000000000" のため ISIN を使用する。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from lxml import etree

from src.edgar import EdgarClient, normalize_cik


@dataclass
class RawHolding:
    """アダプタが書類から抽出した 1 銘柄分の生データ。"""

    reported_name: str
    shares: float
    value_usd: float
    cusip: str | None = None
    isin: str | None = None
    pct_net_assets: float | None = None


@dataclass
class FilingHoldings:
    """1 基準日分の保有明細セット。"""

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
    """N-PORT-P の primary_doc.xml を解析して普通株ロング保有明細を返す。"""
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

    # 同一識別子の複数ロットがあれば合算する
    merged: dict[str, RawHolding] = {}
    for item in invst_or_secs:
        if _local_name(item.tag) != "invstOrSec":
            continue

        asset_cat = _find_child_text(item, "assetCat").upper()
        payoff = _find_child_text(item, "payoffProfile")
        if asset_cat != "EC" or payoff != "Long":
            continue

        name = _find_child_text(item, "name")
        if not name:
            name = _find_child_text(item, "title")

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

        # N-PORT の pctVal はパーセント表記（例: 4.12 = 4.12%）なので比率（0.0412）に正規化
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


def fetch_etf_nport_filings(
    client: EdgarClient,
    etf_cfg: dict[str, Any],
) -> list[FilingHoldings]:
    """対象 ETF の全 N-PORT-P を取得・解析して基準日昇順で返す。"""
    etf_id = etf_cfg["etf_id"]
    cik = normalize_cik(etf_cfg["cik"])
    series_id = etf_cfg["series_id"]
    patterns = etf_cfg.get("series_name_patterns") or [etf_cfg["name"]]

    all_nport = client.get_all_filings(cik, forms={"NPORT-P", "NPORT-P/A"})
    by_accn = {f["accessionNumber"]: f for f in all_nport}

    # EFTS で対象ファンド名が primary_doc.xml に含まれる提出を絞り込む
    candidate_accns: dict[str, dict[str, str]] = {}
    for idx, pat in enumerate(patterns):
        cache_key = f"{etf_id}_nport_name" if idx == 0 else f"{etf_id}_nport_name_{idx}"
        hits = client.search_efts_accessions(f'"{pat}"', "NPORT-P", cache_key)
        for hit in hits:
            hid = str(hit.get("_id") or "")
            adsh = str((hit.get("_source") or {}).get("adsh") or "")
            if adsh in by_accn and hid.endswith("primary_doc.xml"):
                candidate_accns[adsh] = by_accn[adsh]

    # 同一 reportDate に修正申告（NPORT-P/A）等がある場合は最新の filingDate を優先
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
            # 名前が偶然一致した他シリーズであればスキップし、それ以外は伝播させる
            if "Series ID mismatch" in str(exc):
                continue
            raise

        prev = by_as_of.get(parsed.as_of)
        if prev is None or parsed.filed >= prev.filed:
            by_as_of[parsed.as_of] = parsed

    return [by_as_of[d] for d in sorted(by_as_of)]
