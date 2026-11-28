"""Stock-split inference from comparative SEC 10-K and 10-Q filings."""

from __future__ import annotations

import statistics
from collections import defaultdict
from typing import Any

from src.xbrl.records import (
    FactRecord,
    _duration_days,
    _is_annual_flow,
    _iter_tag_records,
    _parse_date,
)
from src.xbrl.tags import (
    ANNUAL_FORMS,
    KNOWN_SPLIT_RATIOS,
    QUARTERLY_FORMS,
    TAG_MAP_FLOW_PER_SHARE,
    TAG_MAP_FLOW_SHARES,
)


def _snap_split_ratio(ratio: float) -> float:
    """Snap an empirical split ratio to the exact theoretical ratio when sufficiently close."""
    if ratio <= 0:
        return 1.0
    for known in KNOWN_SPLIT_RATIOS:
        if abs(ratio / known - 1.0) <= 0.025:
            return known
    if ratio < 0.01:
        # Preserve significant digits on extreme reverse splits so ratio never rounds to 0.0
        return float(f"{ratio:.4g}")
    return round(ratio, 4)


def _estimate_split_events(
    shares_recs: list[FactRecord],
    eps_recs: list[FactRecord],
    dps_decl_recs: list[FactRecord],
    dps_paid_recs: list[FactRecord],
    shares_out_recs: list[FactRecord] | None = None,
) -> list[tuple[str, float]]:
    """Infer stock split events [(effective_filed, factor), ...] from overlapping 10-K comparative years.

    Each tuple (effective_filed, factor) means:
    per-share figures filed before effective_filed are divided by factor, and share counts
    are multiplied by factor, to align onto the post-effective_filed share basis.
    """

    def index_by_end_and_filed(
        recs: list[FactRecord], require_annual: bool = True
    ) -> dict[str, dict[str, FactRecord]]:
        # Keep the single highest-priority tag record for each (fy_end, filed)
        table: dict[str, dict[str, FactRecord]] = defaultdict(dict)
        for rec in recs:
            if require_annual and not _is_annual_flow(rec):
                continue
            if not require_annual and rec.start is not None:
                continue
            cur = table[rec.end].get(rec.filed)
            if cur is None or rec.priority < cur.priority:
                table[rec.end][rec.filed] = rec
        return table

    shares_idx = index_by_end_and_filed(shares_recs, require_annual=True)
    shares_out_idx = (
        index_by_end_and_filed(shares_out_recs, require_annual=False)
        if shares_out_recs
        else {}
    )
    eps_idx = index_by_end_and_filed(eps_recs, require_annual=True)
    dps_d_idx = index_by_end_and_filed(dps_decl_recs, require_annual=True)
    dps_p_idx = index_by_end_and_filed(dps_paid_recs, require_annual=True)

    # Collect all 10-K filing dates across diluted shares, instant shares, and EPS in ascending order
    all_filings = sorted(
        {
            filed
            for tbl in (shares_idx, shares_out_idx, eps_idx)
            for end_map in tbl.values()
            for filed in end_map.keys()
        }
    )
    if len(all_filings) < 2:
        return []

    def _corroborate_ratio(fy_end: str, prev_f: str, next_f: str, r_shares: float) -> float | None:
        """Verify share ratio against inverse EPS/DPS ratio (also handling 1,000x/1,000,000x unit switches)."""
        # Collect candidate inverse per-share ratios for this (fy_end, prev_f -> next_f)
        inv_ratios: list[float] = []
        e_old_rec = eps_idx.get(fy_end, {}).get(prev_f)
        e_new_rec = eps_idx.get(fy_end, {}).get(next_f)
        if e_old_rec and e_new_rec:
            e_old, e_new = e_old_rec.val, e_new_rec.val
            if e_old * e_new > 0 and abs(e_new) >= 0.05:
                inv_ratios.append(e_old / e_new)
        for d_idx in (dps_d_idx, dps_p_idx):
            d_old_rec = d_idx.get(fy_end, {}).get(prev_f)
            d_new_rec = d_idx.get(fy_end, {}).get(next_f)
            if d_old_rec and d_new_rec and d_old_rec.val > 0 and d_new_rec.val > 0:
                inv_ratios.append(d_old_rec.val / d_new_rec.val)

        for r_ps in inv_ratios:
            if 0.92 <= r_ps <= 1.08:
                continue
            if abs(r_shares / r_ps - 1.0) <= 0.08:
                return r_shares
            # Allow slightly wider tolerance for large splits/reverse-splits (>= 3x or <= 1/3x)
            # when a simultaneous merger/split-off modestly shifts comparative net income (e.g. SIRI 1:10 in 2024)
            if (r_shares >= 3.0 or r_shares <= 0.34) and abs(r_shares / r_ps - 1.0) <= 0.16:
                return r_shares
            # Handle simultaneous 1,000x / 1,000,000x share unit switch in the same 10-K (e.g. OKE 2012)
            for scale in (1_000.0, 1_000_000.0, 1e-3, 1e-6):
                r_scaled = r_shares / scale
                if abs(r_scaled / r_ps - 1.0) <= 0.05:
                    return r_ps
        return None

    events: list[tuple[str, float]] = []

    # Compare overlapping fy_ends between consecutive filing dates (prev_filed -> next_filed)
    for idx in range(len(all_filings) - 1):
        prev_f = all_filings[idx]
        next_f = all_filings[idx + 1]

        confirmed_ratios: list[float] = []
        candidate_ends = set(shares_idx.keys()) | set(shares_out_idx.keys())
        for fy_end in candidate_ends:
            matched_ratio: float | None = None
            for sh_tbl in (shares_idx, shares_out_idx):
                end_shares = sh_tbl.get(fy_end)
                if not end_shares or prev_f not in end_shares or next_f not in end_shares:
                    continue
                s_old = end_shares[prev_f].val
                s_new = end_shares[next_f].val
                if s_old <= 0 or s_new <= 0:
                    continue
                r_shares = s_new / s_old
                if 0.92 <= r_shares <= 1.08:
                    continue
                matched_ratio = _corroborate_ratio(fy_end, prev_f, next_f, r_shares)
                if matched_ratio is not None:
                    break

            if matched_ratio is not None:
                confirmed_ratios.append(matched_ratio)

        if confirmed_ratios:
            raw_ratio = statistics.median(confirmed_ratios)
            snapped = _snap_split_ratio(raw_ratio)
            if abs(snapped - 1.0) > 0.05:
                events.append((next_f, snapped))

    return events


def _detect_post_10k_split(
    company_facts: dict[str, Any],
    annual_shares_recs: list[FactRecord],
    annual_eps_recs: list[FactRecord],
) -> tuple[str, float] | None:
    """Detect a mid-year stock split occurring after the latest 10-K via comparative 10-Q filings."""
    latest_10k_filed = max(
        (r.filed for r in (annual_shares_recs + annual_eps_recs) if r.filed),
        default=None,
    )
    if not latest_10k_filed:
        return None

    q_sh_recs = _iter_tag_records(
        company_facts, TAG_MAP_FLOW_SHARES["shares_diluted"], ("shares",), QUARTERLY_FORMS
    )
    q_eps_recs = _iter_tag_records(
        company_facts, TAG_MAP_FLOW_PER_SHARE["eps_diluted"], ("USD/shares",), QUARTERLY_FORMS
    )

    def _index_q_periods(recs: list[FactRecord]) -> dict[tuple[str, str], dict[str, FactRecord]]:
        tbl: dict[tuple[str, str], dict[str, FactRecord]] = defaultdict(dict)
        for rec in recs:
            dur = _duration_days(rec.start, rec.end)
            if dur is None or not (60 <= dur <= 310) or not rec.start:
                continue
            cur = tbl[(rec.start, rec.end)].get(rec.filed)
            if cur is None or rec.priority < cur.priority:
                tbl[(rec.start, rec.end)][rec.filed] = rec
        return tbl

    q_sh_idx = _index_q_periods(q_sh_recs)
    q_eps_idx = _index_q_periods(q_eps_recs)
    post_ratios: list[float] = []
    post_filings: list[str] = []

    for period_key, sh_by_filed in q_sh_idx.items():
        pre_filings = [f for f in sh_by_filed if f <= latest_10k_filed]
        after_filings = [f for f in sh_by_filed if f > latest_10k_filed]
        if not pre_filings or not after_filings:
            continue
        pre_f = max(pre_filings)
        post_f = max(after_filings)
        old_sh = sh_by_filed[pre_f].val
        new_sh = sh_by_filed[post_f].val
        if old_sh <= 0 or new_sh <= 0:
            continue
        r_sh = new_sh / old_sh
        # Check EPS corroboration for the same comparative period (also handles 1e3x/1e6x share unit switches)
        eps_map = q_eps_idx.get(period_key, {})
        old_e_rec = eps_map.get(pre_f)
        new_e_rec = eps_map.get(post_f)
        if (
            old_e_rec is not None
            and new_e_rec is not None
            and abs(old_e_rec.val) >= 0.10
            and abs(new_e_rec.val) >= 0.02
            and (old_e_rec.val * new_e_rec.val > 0)
        ):
            r_eps_inv = old_e_rec.val / new_e_rec.val
            if 0.80 <= r_eps_inv <= 1.25:
                continue
            # Normalize 1e3x/1e6x unit switch in shares if inverse EPS confirms a real split
            tol = 0.16 if abs(new_e_rec.val) < 0.25 else 0.06
            cand_r = r_sh
            for mult in (1.0, 1_000.0, 1_000_000.0, 1e-3, 1e-6):
                scaled = r_sh * mult
                if abs(scaled / r_eps_inv - 1.0) <= tol:
                    cand_r = scaled
                    break
            if abs(cand_r / r_eps_inv - 1.0) <= tol and (cand_r >= 1.25 or cand_r <= 0.80):
                post_ratios.append(cand_r)
                # Record earliest post-10K filing that already uses the split-adjusted share basis
                for af in sorted(after_filings):
                    af_r = sh_by_filed[af].val / old_sh
                    if any(abs((af_r * m) / r_eps_inv - 1.0) <= tol for m in (1.0, 1e3, 1e6, 1e-3, 1e-6)):
                        post_filings.append(af)
                        break

    # Fallback for issuers that omit quarterly EPS from 10-Qs (e.g. CVNA 5:1 split in Q2 2026):
    # compare post-10-K 10-Q comparative prior-FY YTD basic shares against the latest 10-K annual basic shares.
    if not post_ratios:
        has_post_q_eps = any(
            f > latest_10k_filed for eps_by_f in q_eps_idx.values() for f in eps_by_f
        )
        if not has_post_q_eps:
            basic_10k = [
                r
                for r in _iter_tag_records(
                    company_facts,
                    [("us-gaap", "WeightedAverageNumberOfSharesOutstandingBasic")],
                    ("shares",),
                    ANNUAL_FORMS,
                )
                if _is_annual_flow(r) and r.val > 0
            ]
            if basic_10k:
                latest_basic_10k = max(basic_10k, key=lambda r: (r.end, r.filed))
                d_fys = _parse_date(latest_basic_10k.start)
                d_fye = _parse_date(latest_basic_10k.end)
                if d_fys is not None and d_fye is not None:
                    basic_q = _iter_tag_records(
                        company_facts,
                        [("us-gaap", "WeightedAverageNumberOfSharesOutstandingBasic")],
                        ("shares",),
                        QUARTERLY_FORMS,
                    )
                    comp_pts: list[tuple[float, str]] = []
                    cur_pts: list[float] = []
                    for r in basic_q:
                        if r.filed <= latest_basic_10k.filed or r.val <= 0 or not r.start:
                            continue
                        dur = _duration_days(r.start, r.end)
                        if dur is None or not (150 <= dur <= 305):
                            continue
                        ds, de = _parse_date(r.start), _parse_date(r.end)
                        if ds is None or de is None:
                            continue
                        if abs((ds - d_fys).days) <= 12 and de <= d_fye:
                            comp_pts.append((r.val / latest_basic_10k.val, r.filed))
                        elif ds > d_fye:
                            cur_pts.append(r.val / latest_basic_10k.val)
                    if comp_pts and cur_pts:
                        med_comp = statistics.median(x[0] for x in comp_pts)
                        for known in KNOWN_SPLIT_RATIOS:
                            if (
                                (known >= 2.0 or known <= 0.5)
                                and all(abs(r / known - 1.0) <= 0.03 for r, _ in comp_pts)
                                and all(abs(r / known - 1.0) <= 0.15 for r in cur_pts)
                                and abs(med_comp / known - 1.0) <= 0.025
                            ):
                                post_ratios.append(known)
                                post_filings.append(min(f for _, f in comp_pts))
                                break

    if not post_ratios or not post_filings:
        return None

    factor = _snap_split_ratio(statistics.median(post_ratios))
    eff_filed = min(post_filings)
    # Confirm the latest 10-K itself is still on the pre-split share basis
    # (prevents double-applying a split already reflected in the latest 10-K, e.g. AMCR FY2026)
    latest_10k_sh_recs = [
        r
        for r in annual_shares_recs
        if _is_annual_flow(r) and r.filed == latest_10k_filed and r.val > 0
    ]
    latest_post_q_recs = [
        r
        for r in q_sh_recs
        if r.filed >= eff_filed
        and r.val > 0
        and _duration_days(r.start, r.end) is not None
        and 60 <= (_duration_days(r.start, r.end) or 0) <= 310
    ]
    already_in_10k = False
    if latest_10k_sh_recs and latest_post_q_recs:
        k_sh = max(latest_10k_sh_recs, key=lambda r: (r.end, -r.priority)).val
        q_sh = max(latest_post_q_recs, key=lambda r: (r.end, r.filed, -r.priority)).val
        q_vs_k = q_sh / k_sh
        norm_q_vs_k = min(
            (q_vs_k * m for m in (1.0, 1e3, 1e6, 1e-3, 1e-6)),
            key=lambda v: abs(v - 1.0),
        )
        if 0.70 <= norm_q_vs_k <= 1.40:
            already_in_10k = True

    if not already_in_10k and (factor >= 1.25 or factor <= 0.80):
        return (eff_filed, factor)
    return None


def _refine_quarterly_split_events(
    company_facts: dict[str, Any],
    split_events: list[tuple[str, float]],
) -> list[tuple[str, float]]:
    """Refine split effective dates using 10-Q comparative EPS within the preceding ~1 year.

    Prevents double-dividing 10-Qs filed AFTER a mid-year split and BEFORE the next 10-K
    (e.g. CTAS 4:1 split in Sep 2024 before Jul 2025 10-K), while bounding the search window
    so serial splitters (e.g. APH, MNST, SFBS) never collide with earlier splits.
    """
    q_split_events = list(split_events)
    if not q_split_events:
        return q_split_events

    raw_q_eps_all = _iter_tag_records(
        company_facts, TAG_MAP_FLOW_PER_SHARE["eps_diluted"], ("USD/shares",), QUARTERLY_FORMS
    )
    eps_by_period: dict[tuple[str, str], dict[str, FactRecord]] = defaultdict(dict)
    for rec in raw_q_eps_all:
        if not rec.start:
            continue
        dur = _duration_days(rec.start, rec.end)
        if dur is None or not (60 <= dur <= 310):
            continue
        cur = eps_by_period[(rec.start, rec.end)].get(rec.filed)
        if cur is None or rec.priority < cur.priority:
            eps_by_period[(rec.start, rec.end)][rec.filed] = rec

    refined_events: list[tuple[str, float]] = []
    prev_eff_bound: str | None = None
    for eff_filed_10k, factor in sorted(q_split_events, key=lambda item: item[0]):
        d_eff = _parse_date(eff_filed_10k)
        earliest_q_post: str | None = None
        for by_filed in eps_by_period.values():
            filings_sorted = sorted(by_filed.keys())
            for i in range(len(filings_sorted)):
                f_old = filings_sorted[i]
                if f_old >= eff_filed_10k:
                    break
                v_old = by_filed[f_old].val
                if abs(v_old) < 0.15:
                    continue
                for j in range(i + 1, len(filings_sorted)):
                    f_new = filings_sorted[j]
                    if f_new >= eff_filed_10k:
                        break
                    if prev_eff_bound is not None and f_new <= prev_eff_bound:
                        continue
                    d_f_new = _parse_date(f_new)
                    if d_eff is not None and d_f_new is not None and (d_eff - d_f_new).days > 380:
                        continue
                    v_new = by_filed[f_new].val
                    if abs(v_new) < 0.03 or v_old * v_new <= 0:
                        continue
                    r_inv = v_old / v_new
                    if abs(r_inv / factor - 1.0) <= 0.08:
                        if earliest_q_post is None or f_new < earliest_q_post:
                            earliest_q_post = f_new
                        break
        refined_events.append((earliest_q_post or eff_filed_10k, factor))
        prev_eff_bound = eff_filed_10k
    return sorted(refined_events, key=lambda item: item[0])


def _cumulative_split_factor(filed: str | None, split_events: list[tuple[str, float]]) -> float:
    """Return the cumulative split factor from the given filing date to the latest filing."""
    if not filed or not split_events:
        return 1.0
    factor = 1.0
    for effective_filed, ratio in split_events:
        if filed < effective_filed and ratio > 0:
            factor *= ratio
    return factor if factor > 0 else 1.0
