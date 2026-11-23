"""Schwab 旧書類（N-Q / N-CSR / N-CSRS）HTML アダプタ（2011年11月〜2019年8月）。

AGENTS.md ルール:
- 全銘柄表（"Portfolio Holdings as of …"）のみ採用し、上位 50 銘柄の Condensed 版を除外する。
- 解析結果の評価額合計が書類の Total Common Stock と 1 ドル以内で一致しなければ例外とする検証を外さない。
- 2019 年 5 月末の SCHD は公開書類がない既知の欠落。
"""

from __future__ import annotations

import re
from typing import Any

from lxml import html as lhtml

from src.adapters.nport import FilingHoldings, RawHolding
from src.edgar import EdgarClient, normalize_cik

_SPACE_RE = re.compile(r"\s+")
_NUM_RE = re.compile(r"^\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)$")
# 銘柄名末尾の脚注マーカー（例: (a), (b), *, †, ‡ 等）を除去（Class A 等は残す）
_FOOTNOTE_SUFFIX_RE = re.compile(
    r"(?:\s*(?:\([a-z0-9,\s]+\)|[*†‡§^]))+$"
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
    )
    return _SPACE_RE.sub(" ", cleaned).strip()


def _parse_number(cell: str) -> float | None:
    m = _NUM_RE.match(cell.strip())
    if not m:
        return None
    return float(m.group(1).replace(",", ""))


def _clean_security_name(raw_name: str) -> str:
    name = _clean_text(raw_name)
    # 末尾の脚注記号を繰り返し除去
    prev = None
    while prev != name:
        prev = name
        name = _FOOTNOTE_SUFFIX_RE.sub("", name).strip()
    return name


def _find_full_holdings_section_start(html_text: str, fund_name: str) -> int:
    """全銘柄表（Condensed 版を除く Portfolio Holdings as of ...）の開始オフセットを返す。"""
    # タグ・商標記号・空白、および "U.S." / "US" の表記揺れを許容してファンド名を検索
    word_pats: list[str] = []
    for w in fund_name.split():
        if w.upper() in ("U.S.", "US"):
            word_pats.append(r"U\.?S\.?")
        else:
            word_pats.append(re.escape(w))
    name_pat = r"(?:<[^>]+>|&nbsp;|&#160;|&#153;|&#8482;|&trade;|™|\s)+".join(word_pats)
    pattern = re.compile(name_pat, re.IGNORECASE)

    candidates: list[tuple[int, int]] = []
    for m in pattern.finditer(html_text):
        tail = html_text[m.end() : m.end() + 800]
        tail_plain = _clean_text(re.sub(r"<[^>]+>|&nbsp;|&#160;|&#153;|&#8482;|&trade;", " ", tail))
        tail_low = tail_plain.lower()

        # Condensed 版（上位 50 銘柄サマリー）は必ず除外する
        if "condensed" in tail_low[:80]:
            continue
        # ファンド名の直後に Portfolio Holdings が続く見出しのみ採用（監査報告書本文等を除外）
        if "portfolio holdings" not in tail_low[:60]:
            continue
        # 2 ページ目以降の "continued" 見出しは開始点にしない
        if "continued" in tail_low[:80]:
            continue
        if "as of" in tail_low[:160]:
            # as of を含む正式な開始見出しを最優先
            candidates.append((0, m.start()))
        else:
            candidates.append((1, m.start()))

    if not candidates:
        raise ValueError(f"Full 'Portfolio Holdings as of ...' section not found for {fund_name}")

    # 同一ファイル内で最も優先度が高く、後ろ（正式明細セクション）にある見出しを採用
    min_prio = min(c[0] for c in candidates)
    best_positions = [pos for prio, pos in candidates if prio == min_prio]
    return best_positions[-1]


def parse_schwab_legacy_html(
    html_bytes: bytes,
    etf_id: str,
    fund_name: str,
    as_of: str,
    form: str,
    accession: str,
    filed: str,
) -> FilingHoldings:
    """Schwab 旧書類 HTML から SCHD の全普通株明細を抽出し、Total Common Stock と照合する。"""
    html_text = html_bytes.decode("utf-8", errors="replace")
    start_pos = _find_full_holdings_section_start(html_text, fund_name)

    # 1 ファンド分の全銘柄表は通常 150〜300KB 以内に収まるため、十分な幅を切り出す
    # ただし次のファンドの "Portfolio Holdings as of" があればそこで打ち切る
    chunk = html_text[start_pos : start_pos + 700_000]
    doc = lhtml.fromstring(chunk)

    net_assets: float | None = None
    in_common_stock = False
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

        # 先頭サマリー表から Net Assets を取得（pct_net_assets 算出用）
        if not in_common_stock and "net assets" in row_low and "of net assets" not in row_low:
            last_num = _parse_number(cells[-1])
            if last_num is not None and last_num > 0:
                net_assets = last_num

        # 普通株セクションの開始検知（例: "Common Stock 99.6% of net assets"）
        if not in_common_stock:
            if row_low.startswith("common stock") and "of net assets" in row_low:
                in_common_stock = True
            continue

        # Total Common Stock 行（1 行型・2 行型の両方に対応）
        if waiting_total_value:
            val_num = _parse_number(cells[-1])
            if val_num is not None and val_num > 0:
                reported_total = val_num
                break
            waiting_total_value = False

        if "total common stock" in row_low:
            # 同じ行の末尾セルに評価額がある場合
            val_num = _parse_number(cells[-1])
            if val_num is not None and val_num > 0:
                reported_total = val_num
                break
            # 次行に "(Cost $...) | <Value>" がある 2 行型（2011〜2014 年形式）
            waiting_total_value = True
            continue

        # 別の資産クラス（Other Investment Company 等）や次セクションに入ったら終了
        if row_low.startswith(("other investment compan", "short-term investment", "total investments")):
            break

        # 銘柄行の抽出: 先頭が社名、末尾 2 セルが株数・評価額
        if len(cells) >= 3:
            val_num = _parse_number(cells[-1])
            sh_num = _parse_number(cells[-2])
            if sh_num is not None and val_num is not None and sh_num > 0 and val_num > 0:
                # 銘柄名セル群（末尾 2 セルを除く）を結合
                name_part = " ".join(cells[:-2])
                name_low = name_part.lower()
                # ヘッダや小計行を除外
                if any(
                    skip in name_low
                    for skip in ("number of shares", "security", "total ", "subtotal")
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
                        pct_net_assets=(val_num / net_assets) if net_assets else None,
                    )

    if reported_total is None:
        raise ValueError(
            f"Total Common Stock row not found in {form} {accession} ({as_of})"
        )

    holdings = sorted(merged.values(), key=lambda h: h.value_usd, reverse=True)
    if not holdings:
        raise ValueError(f"No holdings parsed from {form} {accession} ({as_of})")

    calc_total = sum(h.value_usd for h in holdings)
    diff = abs(calc_total - reported_total)
    # AGENTS.md 必須検証: Total Common Stock と 1 ドル以内で一致しなければ例外
    if diff > 1.0:
        raise ValueError(
            f"Total Common Stock mismatch for {etf_id} {as_of} ({accession}): "
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


def fetch_schwab_legacy_filings(
    client: EdgarClient,
    etf_cfg: dict[str, Any],
    max_as_of: str = "2019-09-30",
) -> list[FilingHoldings]:
    """Schwab 旧書類（N-Q / N-CSR / N-CSRS）をすべて取得・解析して返す。"""
    etf_id = etf_cfg["etf_id"]
    cik = normalize_cik(etf_cfg["cik"])
    fund_name = etf_cfg["name"]
    fy_end_month = int(etf_cfg.get("fiscal_year_end_month") or 8)

    # 決算月（例: 8月）に応じた報告書種別マッピング
    semiannual_month = ((fy_end_month + 5) % 12) + 1  # 8月決算 → 2月
    q1_month = ((fy_end_month + 2) % 12) + 1          # 8月決算 → 11月
    q3_month = ((fy_end_month + 8) % 12) + 1          # 8月決算 → 5月

    all_legacy = client.get_all_filings(
        cik, forms={"N-CSR", "N-CSR/A", "N-CSRS", "N-CSRS/A", "N-Q", "N-Q/A"}
    )
    by_accn = {
        f["accessionNumber"]: f
        for f in all_legacy
        if f["reportDate"] and f["reportDate"] <= max_as_of
    }

    hits = client.search_efts_accessions(
        f'"{fund_name}"', "N-Q,N-CSR,N-CSRS", f"{etf_id}_legacy_name"
    )

    selected_by_date: dict[str, dict[str, str]] = {}
    for hit in hits:
        adsh = str((hit.get("_source") or {}).get("adsh") or "")
        if adsh not in by_accn:
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

        prev = selected_by_date.get(rd)
        if prev is None or meta["filingDate"] >= prev["filingDate"]:
            selected_by_date[rd] = meta

    results: list[FilingHoldings] = []
    for rd in sorted(selected_by_date):
        meta = selected_by_date[rd]
        html_bytes = client.get_filing_document(
            cik, meta["accessionNumber"], meta["primaryDocument"]
        )
        parsed = parse_schwab_legacy_html(
            html_bytes=html_bytes,
            etf_id=etf_id,
            fund_name=fund_name,
            as_of=rd,
            form=meta["form"],
            accession=meta["accessionNumber"],
            filed=meta["filingDate"],
        )
        results.append(parsed)

    return results
