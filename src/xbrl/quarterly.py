"""TTM and standalone quarterly SEC XBRL financials extraction (including Q4 derivation)."""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Any

from src.xbrl.records import (
    FactRecord,
    _duration_days,
    _fix_share_scale,
    _is_annual_flow,
    _iter_tag_records,
    _parse_date,
    _rec_recency_key,
    _select_best_instant_for_dates,
    _select_best_single_quarter,
    _select_best_ytd_by_end,
)
from src.xbrl.splits import _cumulative_split_factor, _refine_quarterly_split_events
from src.xbrl.tags import (
    ANNUAL_FORMS,
    BANK_NII_TAGS,
    BANK_NONINT_TAGS,
    QUARTERLY_FORMS,
    REIT_LEASE_TAGS,
    TAG_MAP_FLOW_PER_SHARE,
    TAG_MAP_FLOW_SHARES,
    TAG_MAP_FLOW_USD,
    TAG_MAP_INSTANT_USD,
)


def _find_latest_ytd_pair(
    records: list[FactRecord],
    latest_fy_start: str | None,
    latest_fy_end: str,
    target_q_end: str | None = None,
    require_same_tag: bool = False,
) -> tuple[str | None, float | None, float | None]:
    """Return (q_end, cur_ytd, prev_ytd) for 10-Q YTD after the latest 10-K and its prior-year counterpart."""
    d_fy_end = _parse_date(latest_fy_end)
    d_fy_start = _parse_date(latest_fy_start)
    if d_fy_end is None:
        return None, None, None

    # Current YTD candidates: start near latest_fy_end (-7 to +10d), end > latest_fy_end, duration 70-305d
    cur_candidates: list[FactRecord] = []
    for rec in records:
        d_s = _parse_date(rec.start)
        d_e = _parse_date(rec.end)
        if d_s is None or d_e is None:
            continue
        if target_q_end is not None and rec.end != target_q_end:
            continue
        if d_e <= d_fy_end:
            continue
        start_gap = (d_s - d_fy_end).days
        dur = (d_e - d_s).days
        if -7 <= start_gap <= 10 and 70 <= dur <= 305:
            cur_candidates.append(rec)

    if not cur_candidates:
        return None, None, None

    # Pick the latest end date, highest-priority tag, and latest filing
    latest_q_end = max(r.end for r in cur_candidates)
    q_recs = [r for r in cur_candidates if r.end == latest_q_end]
    q_recs.sort(key=lambda r: (r.priority, tuple(-ord(c) for c in r.filed)))
    # Latest filing within the highest-priority tag
    best_prio = min(r.priority for r in q_recs)
    cur_rec = max(
        (r for r in q_recs if r.priority == best_prio),
        key=_rec_recency_key,
    )
    cur_dur = _duration_days(cur_rec.start, cur_rec.end)
    if cur_dur is None:
        return None, None, None

    # Prior-year YTD: same tag (preferred), start near latest_fy_start (+/-10d), duration within cur_dur +/-15d
    prev_candidates: list[FactRecord] = []
    for rec in records:
        d_s = _parse_date(rec.start)
        d_e = _parse_date(rec.end)
        if d_s is None or d_e is None:
            continue
        dur = (d_e - d_s).days
        if abs(dur - cur_dur) > 15:
            continue
        if d_fy_start is not None:
            if abs((d_s - d_fy_start).days) > 12:
                continue
        else:
            # If fy_start is unknown, match end date ~1 year prior to latest_q_end (350-380d earlier)
            d_q_end = _parse_date(latest_q_end)
            if d_q_end is None or not (345 <= (d_q_end - d_e).days <= 385):
                continue
        prev_candidates.append(rec)

    if not prev_candidates:
        return latest_q_end, None, None

    # Prefer same accession and tag as current YTD; fall back to latest filing with the same tag
    same_accn = [r for r in prev_candidates if r.accn == cur_rec.accn and r.tag == cur_rec.tag]
    if same_accn:
        prev_rec = same_accn[0]
    else:
        same_tag = [r for r in prev_candidates if r.tag == cur_rec.tag]
        if require_same_tag and not same_tag:
            return latest_q_end, None, None
        pool = same_tag or prev_candidates
        min_p = min(r.priority for r in pool)
        prev_rec = max((r for r in pool if r.priority == min_p), key=_rec_recency_key)

    return latest_q_end, cur_rec.val, prev_rec.val


def extract_ttm_financials(
    company_facts: dict[str, Any],
    annual_rows: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Compute TTM (trailing twelve months) metrics from the latest 10-K and subsequent 10-Q."""
    if not annual_rows:
        return None

    latest_ann = annual_rows[-1]
    latest_fy_end = latest_ann["fy_end"]
    latest_fy_start = latest_ann.get("fy_start")

    # Identify the latest 10-Q period_end from core flow metrics (revenue / net_income / operating_cf)
    q_rev_recs = _iter_tag_records(company_facts, TAG_MAP_FLOW_USD["revenue"], ("USD",), QUARTERLY_FORMS)
    q_ni_recs = _iter_tag_records(company_facts, TAG_MAP_FLOW_USD["net_income"], ("USD",), QUARTERLY_FORMS)
    q_ocf_recs = _iter_tag_records(company_facts, TAG_MAP_FLOW_USD["operating_cf"], ("USD",), QUARTERLY_FORMS)

    detected_q_ends: list[str] = []
    for recs in (q_rev_recs, q_ni_recs, q_ocf_recs):
        q_end, cur_v, prev_v = _find_latest_ytd_pair(recs, latest_fy_start, latest_fy_end)
        if q_end and cur_v is not None and prev_v is not None:
            detected_q_ends.append(q_end)

    target_q_end = max(detected_q_ends) if detected_q_ends else None
    period_end = target_q_end or latest_fy_end

    def calc_ttm_usd(
        metric: str,
        abs_outflow: bool = False,
        require_same_tag: bool = False,
    ) -> tuple[float | None, bool]:
        ann_val = latest_ann.get(metric)
        if ann_val is None:
            return None, False
        if target_q_end is None:
            return ann_val, False
        recs = _iter_tag_records(company_facts, TAG_MAP_FLOW_USD[metric], ("USD",), QUARTERLY_FORMS)
        if metric == "revenue":
            nonzero = [r for r in recs if r.val != 0.0]
            if nonzero:
                recs = nonzero
        _, cur_v, prev_v = _find_latest_ytd_pair(
            recs,
            latest_fy_start,
            latest_fy_end,
            target_q_end,
            require_same_tag=require_same_tag,
        )
        if cur_v is None or prev_v is None:
            return ann_val, False
        if abs_outflow:
            cur_v = abs(cur_v)
            prev_v = abs(prev_v)
        return ann_val + cur_v - prev_v, True

    def calc_ttm_ps(metric: str) -> float | None:
        ann_val = latest_ann.get(metric)
        if ann_val is None:
            return None
        if target_q_end is None:
            return ann_val
        recs = _iter_tag_records(
            company_facts, TAG_MAP_FLOW_PER_SHARE[metric], ("USD/shares",), QUARTERLY_FORMS
        )
        _, cur_v, prev_v = _find_latest_ytd_pair(recs, latest_fy_start, latest_fy_end, target_q_end)
        if cur_v is None or prev_v is None:
            return ann_val
        return round(ann_val + cur_v - prev_v, 4)

    revenue_ttm, _ = calc_ttm_usd("revenue")
    # Bank revenue synthesis for TTM when 10-Q omits Revenues or only tags ASC 606 fee income
    if target_q_end is not None and latest_ann.get("revenue") is not None:
        q_nii = _iter_tag_records(company_facts, BANK_NII_TAGS, ("USD",), QUARTERLY_FORMS)
        q_nonint = _iter_tag_records(company_facts, BANK_NONINT_TAGS, ("USD",), QUARTERLY_FORMS)
        _, cur_nii, prev_nii = _find_latest_ytd_pair(q_nii, latest_fy_start, latest_fy_end, target_q_end)
        _, cur_ni2, prev_ni2 = _find_latest_ytd_pair(q_nonint, latest_fy_start, latest_fy_end, target_q_end)
        if None not in (cur_nii, prev_nii, cur_ni2, prev_ni2) and cur_nii > 0:
            bank_cur = cur_nii + cur_ni2
            bank_prev = prev_nii + prev_ni2
            _, cur_r_std, prev_r_std = _find_latest_ytd_pair(
                q_rev_recs, latest_fy_start, latest_fy_end, target_q_end
            )
            if cur_r_std is None or prev_r_std is None or bank_cur > max(0.0, cur_r_std) * 1.5:
                revenue_ttm = latest_ann["revenue"] + bank_cur - bank_prev
    # REIT lease revenue TTM fallback when 10-Q only reports ASC 606 non-lease fee revenue
    if target_q_end is not None and latest_ann.get("revenue") is not None:
        q_lease = _iter_tag_records(company_facts, REIT_LEASE_TAGS, ("USD",), QUARTERLY_FORMS)
        _, cur_l, prev_l = _find_latest_ytd_pair(q_lease, latest_fy_start, latest_fy_end, target_q_end)
        if cur_l is not None and prev_l is not None and cur_l > 0:
            _, cur_r_std, prev_r_std = _find_latest_ytd_pair(
                q_rev_recs, latest_fy_start, latest_fy_end, target_q_end
            )
            if cur_r_std is None or prev_r_std is None:
                revenue_ttm = latest_ann["revenue"] + cur_l - prev_l
            elif cur_l > max(0.0, cur_r_std) * 2.0:
                revenue_ttm = latest_ann["revenue"] + (cur_l + cur_r_std) - (prev_l + prev_r_std)
    op_inc_ttm, _ = calc_ttm_usd("operating_income")
    net_inc_ttm, _ = calc_ttm_usd("net_income")
    ocf_ttm, ocf_updated = calc_ttm_usd("operating_cf", require_same_tag=True)
    capex_ttm, capex_updated = calc_ttm_usd("capex", abs_outflow=True, require_same_tag=True)
    # Require matching update status between OCF and CapEx so TTM FCF never mixes Q3 OCF with prior-FY CapEx
    if ocf_ttm is not None and capex_ttm is not None and (ocf_updated == capex_updated):
        fcf_ttm: float | None = ocf_ttm - capex_ttm
    else:
        fcf_ttm = latest_ann.get("fcf") if not ocf_updated else None
    eps_ttm = calc_ttm_ps("eps_diluted")

    # TTM DPS: prefer declared basis; fall back to paid basis, rejecting 0.0 placeholders & 1,000x+ errors
    dps_ttm = calc_ttm_ps("dps_declared")
    ann_ref_dps = latest_ann.get("dps_declared") or latest_ann.get("dps_paid")
    if dps_ttm is None or (dps_ttm == 0.0 and ann_ref_dps is not None and ann_ref_dps > 0.05):
        dps_ttm = calc_ttm_ps("dps_paid")
    if dps_ttm is not None:
        if dps_ttm < 0 or dps_ttm > 150.0:
            dps_ttm = None
        elif ann_ref_dps is not None and ann_ref_dps > 0 and dps_ttm > 25.0 and (dps_ttm / ann_ref_dps) > 20.0:
            dps_ttm = None
        elif dps_ttm == 0.0 and ann_ref_dps is not None and ann_ref_dps > 0.05:
            dps_ttm = None

    # Balance sheet (equity and period-end shares): fetch latest instant at period_end
    all_forms = ANNUAL_FORMS | QUARTERLY_FORMS
    eq_map = _select_best_instant_for_dates(
        _iter_tag_records(company_facts, TAG_MAP_INSTANT_USD["total_equity"], ("USD",), all_forms),
        [period_end],
    )
    sh_bs_map = _select_best_instant_for_dates(
        _iter_tag_records(
            company_facts, [("us-gaap", "CommonStockSharesOutstanding")], ("shares",), all_forms
        ),
        [period_end],
        max_day_diff_before=15,
        max_day_diff_after=15,
    )
    sh_dei_map = _select_best_instant_for_dates(
        _iter_tag_records(
            company_facts, [("dei", "EntityCommonStockSharesOutstanding")], ("shares",), all_forms
        ),
        [period_end],
        max_day_diff_before=10,
        max_day_diff_after=75,
    )

    total_equity = eq_map[period_end].val if period_end in eq_map else latest_ann.get("total_equity")
    sh_rec = sh_bs_map.get(period_end) or sh_dei_map.get(period_end)
    shares_out = (
        sh_rec.val
        if (sh_rec is not None and sh_rec.val > 0)
        else latest_ann.get("shares_outstanding")
    )
    # Fall back to latest diluted shares when period-end instant is absent (e.g. dual-class issuers like UPS)
    if shares_out is None or shares_out <= 0:
        q_sh_recs = _iter_tag_records(
            company_facts, TAG_MAP_FLOW_SHARES["shares_diluted"], ("shares",), all_forms
        )
        matching_sh = [r for r in q_sh_recs if r.end == period_end and r.val > 0]
        if matching_sh:
            min_p = min(r.priority for r in matching_sh)
            shares_out = max(
                (r for r in matching_sh if r.priority == min_p),
                key=_rec_recency_key,
            ).val
        else:
            shares_out = latest_ann.get("shares_diluted")
    if shares_out is not None and shares_out <= 0:
        shares_out = None

    # Correct 1,000x / 1,000,000x XBRL unit scale errors in TTM period-end shares against latest annual shares
    ann_ref_sh = latest_ann.get("shares_outstanding") or latest_ann.get("shares_diluted")
    shares_out = _fix_share_scale(shares_out, ann_ref_sh)

    return {
        "period_end": period_end,
        "revenue_ttm": revenue_ttm,
        "operating_income_ttm": op_inc_ttm,
        "net_income_ttm": net_inc_ttm,
        "eps_ttm": eps_ttm,
        "fcf_ttm": fcf_ttm,
        "dps_ttm": dps_ttm,
        "total_equity": total_equity,
        "shares_outstanding": shares_out,
    }


def _override_partial_plain_rev(
    selected: dict[str, FactRecord],
    pool: list[FactRecord],
    min_dur: int,
    max_dur: int,
) -> None:
    """Override partial us-gaap:Revenues with ASC 606 total when Revenues is consistently a smaller subset (e.g. OWL)."""
    by_end_tag: dict[tuple[str, str], FactRecord] = {}
    for rec in pool:
        dur = _duration_days(rec.start, rec.end)
        if dur is None or not (min_dur <= dur <= max_dur):
            continue
        k = (rec.end, rec.tag)
        prev = by_end_tag.get(k)
        if prev is None or _rec_recency_key(rec) > _rec_recency_key(prev):
            by_end_tag[k] = rec
    both_ends = [
        e
        for e in selected
        if (e, "us-gaap:Revenues") in by_end_tag
        and (e, "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax") in by_end_tag
    ]
    if not both_ends:
        return
    all_lt = all(
        by_end_tag[(e, "us-gaap:Revenues")].val
        < by_end_tag[(e, "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax")].val
        for e in both_ends
    )
    latest_e = max(selected)
    plain_dropped = (latest_e, "us-gaap:Revenues") not in by_end_tag
    if not (all_lt or plain_dropped):
        return
    for e, cur in list(selected.items()):
        if cur.tag != "us-gaap:Revenues":
            continue
        asc = by_end_tag.get((e, "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"))
        if asc is not None and asc.val > 0 and cur.val > 0 and asc.val > cur.val * 1.55:
            selected[e] = asc


def extract_quarterly_financials(
    company_facts: dict[str, Any],
    annual_rows: list[dict[str, Any]],
    split_events: list[tuple[str, float]],
    max_quarters: int = 16,
) -> list[dict[str, Any]]:
    """Extract up to max_quarters standalone (3-month) PL/CF quarters (deriving Q4 from 10-K minus Q3 YTD)."""
    all_forms = ANNUAL_FORMS | QUARTERLY_FORMS

    raw_rev_all = _iter_tag_records(company_facts, TAG_MAP_FLOW_USD["revenue"], ("USD",), all_forms)
    nonzero_rev_all = [r for r in raw_rev_all if r.val != 0.0]
    rev_pool_all = nonzero_rev_all if nonzero_rev_all else raw_rev_all

    q_rev = _select_best_single_quarter(rev_pool_all)
    _override_partial_plain_rev(q_rev, rev_pool_all, 70, 122)

    # Bank revenue synthesis (standalone quarters): also override ASC 606 fee-only revenue
    q_nii = _select_best_single_quarter(
        _iter_tag_records(company_facts, BANK_NII_TAGS, ("USD",), all_forms)
    )
    q_nonint = _select_best_single_quarter(
        _iter_tag_records(company_facts, BANK_NONINT_TAGS, ("USD",), all_forms)
    )
    for end in set(q_nii) & set(q_nonint):
        r1, r2 = q_nii[end], q_nonint[end]
        bank_tot = r1.val + r2.val
        cur_q = q_rev.get(end)
        use_bank = cur_q is None or (
            r1.val > 0
            and bank_tot > max(0.0, cur_q.val) * 1.5
        )
        if use_bank:
            q_rev[end] = FactRecord(
                val=bank_tot,
                end=end,
                start=r1.start or r2.start,
                filed=max(r1.filed, r2.filed),
                form=r1.form,
                accn=r1.accn,
                tag="synthetic:BankQ",
                priority=99,
            )

    # REIT lease revenue fallback (standalone quarters)
    q_lease = _select_best_single_quarter(
        _iter_tag_records(company_facts, REIT_LEASE_TAGS, ("USD",), all_forms)
    )
    for end, lr in q_lease.items():
        if lr.val <= 0:
            continue
        cur_q = q_rev.get(end)
        if cur_q is None:
            q_rev[end] = lr
        elif "RevenueFromContractWithCustomer" in cur_q.tag and lr.val > max(0.0, cur_q.val) * 2.0:
            q_rev[end] = FactRecord(
                val=lr.val + max(0.0, cur_q.val),
                end=end,
                start=lr.start,
                filed=max(lr.filed, cur_q.filed),
                form=lr.form,
                accn=lr.accn,
                tag="synthetic:LeaseQ",
                priority=98,
            )

    q_op = _select_best_single_quarter(
        _iter_tag_records(company_facts, TAG_MAP_FLOW_USD["operating_income"], ("USD",), all_forms)
    )
    q_pretax = _select_best_single_quarter(
        _iter_tag_records(company_facts, TAG_MAP_FLOW_USD["pretax_income"], ("USD",), all_forms)
    )
    q_ni = _select_best_single_quarter(
        _iter_tag_records(company_facts, TAG_MAP_FLOW_USD["net_income"], ("USD",), all_forms)
    )
    q_eps = _select_best_single_quarter(
        _iter_tag_records(company_facts, TAG_MAP_FLOW_PER_SHARE["eps_diluted"], ("USD/shares",), all_forms)
    )

    # YTD series (used to back-calculate Q4 and compute quarterly OCF/CapEx differences)
    rev_pool_q = [r for r in rev_pool_all if r.form in QUARTERLY_FORMS]
    ytd_rev = _select_best_ytd_by_end(rev_pool_q)
    _override_partial_plain_rev(ytd_rev, rev_pool_q, 70, 305)

    ytd_nii = _select_best_ytd_by_end(
        _iter_tag_records(company_facts, BANK_NII_TAGS, ("USD",), QUARTERLY_FORMS)
    )
    ytd_nonint = _select_best_ytd_by_end(
        _iter_tag_records(company_facts, BANK_NONINT_TAGS, ("USD",), QUARTERLY_FORMS)
    )
    for end in set(ytd_nii) & set(ytd_nonint):
        r1, r2 = ytd_nii[end], ytd_nonint[end]
        if r1.start != r2.start:
            continue
        bank_tot = r1.val + r2.val
        cur_y = ytd_rev.get(end)
        use_bank = cur_y is None or (
            r1.val > 0
            and bank_tot > max(0.0, cur_y.val) * 1.5
        )
        if use_bank:
            ytd_rev[end] = FactRecord(
                val=bank_tot,
                end=end,
                start=r1.start,
                filed=max(r1.filed, r2.filed),
                form=r1.form,
                accn=r1.accn,
                tag="synthetic:BankYTD",
                priority=99,
            )
    ytd_lease = _select_best_ytd_by_end(
        _iter_tag_records(company_facts, REIT_LEASE_TAGS, ("USD",), QUARTERLY_FORMS)
    )
    for end, lr in ytd_lease.items():
        if lr.val <= 0:
            continue
        cur_y = ytd_rev.get(end)
        if cur_y is None:
            ytd_rev[end] = lr
        elif "RevenueFromContractWithCustomer" in cur_y.tag and lr.val > max(0.0, cur_y.val) * 2.0:
            ytd_rev[end] = FactRecord(
                val=lr.val + max(0.0, cur_y.val),
                end=end,
                start=lr.start,
                filed=max(lr.filed, cur_y.filed),
                form=lr.form,
                accn=lr.accn,
                tag="synthetic:LeaseYTD",
                priority=98,
            )

    # Earliest (contemporaneous, pre-restatement) 10-K annual revenue per (fy_end, tag)
    # Used when a later 10-K restated continuing ops downward after a spin-off/divestiture
    # while the historical Q3 10-Q YTD was never restated (e.g. WDC, APP, AIG, MMM)
    orig_ann_rev_by_end_tag: dict[tuple[str, str], FactRecord] = {}
    orig_ann_rev_by_end: dict[str, FactRecord] = {}
    for rec in _iter_tag_records(company_facts, TAG_MAP_FLOW_USD["revenue"], ("USD",), ANNUAL_FORMS):
        if not _is_annual_flow(rec):
            continue
        kt = (rec.end, rec.tag)
        prev_t = orig_ann_rev_by_end_tag.get(kt)
        if prev_t is None or _rec_recency_key(rec) < _rec_recency_key(prev_t):
            orig_ann_rev_by_end_tag[kt] = rec
        prev_e = orig_ann_rev_by_end.get(rec.end)
        if prev_e is None or (rec.priority, _rec_recency_key(rec)) < (prev_e.priority, _rec_recency_key(prev_e)):
            orig_ann_rev_by_end[rec.end] = rec
    ytd_op = _select_best_ytd_by_end(
        _iter_tag_records(company_facts, TAG_MAP_FLOW_USD["operating_income"], ("USD",), QUARTERLY_FORMS)
    )
    ytd_pretax = _select_best_ytd_by_end(
        _iter_tag_records(company_facts, TAG_MAP_FLOW_USD["pretax_income"], ("USD",), QUARTERLY_FORMS)
    )
    ytd_ni = _select_best_ytd_by_end(
        _iter_tag_records(company_facts, TAG_MAP_FLOW_USD["net_income"], ("USD",), QUARTERLY_FORMS)
    )
    ytd_eps = _select_best_ytd_by_end(
        _iter_tag_records(company_facts, TAG_MAP_FLOW_PER_SHARE["eps_diluted"], ("USD/shares",), QUARTERLY_FORMS)
    )
    ytd_ocf = _select_best_ytd_by_end(
        _iter_tag_records(company_facts, TAG_MAP_FLOW_USD["operating_cf"], ("USD",), QUARTERLY_FORMS)
    )
    ytd_capex = _select_best_ytd_by_end(
        _iter_tag_records(company_facts, TAG_MAP_FLOW_USD["capex"], ("USD",), QUARTERLY_FORMS)
    )

    q_split_events = _refine_quarterly_split_events(company_facts, split_events)

    # Build standalone quarter map
    quarters: dict[str, dict[str, Any]] = {}
    all_q_ends = sorted(set(q_rev) | set(q_ni))
    for end in all_q_ends:
        anchor = q_rev.get(end) or q_ni.get(end)
        eps_r = q_eps.get(end)
        eps_val = None
        if eps_r is not None:
            sf = _cumulative_split_factor(eps_r.filed, q_split_events)
            eps_val = round(eps_r.val / sf, 4)

        # Drop placeholder 0.0 quarterly revenue when issuer reports substantial positive annual revenue (e.g. CBSH)
        rev_q_val = q_rev[end].val if end in q_rev else None
        if rev_q_val == 0.0 and any((a.get("revenue") or 0) > 10_000_000 for a in annual_rows):
            rev_q_val = None

        quarters[end] = {
            "period_end": end,
            "period_start": anchor.start if anchor else None,
            "fp": None,
            "revenue": rev_q_val,
            "operating_income": q_op[end].val if end in q_op else None,
            "pretax_income": q_pretax[end].val if end in q_pretax else None,
            "net_income": q_ni[end].val if end in q_ni else None,
            "eps_diluted": eps_val,
            "operating_cf": None,
            "fcf": None,
        }

    # Derive Q4 from annual rows and compute standalone OCF/FCF from intra-year YTD differences
    recent_annual = annual_rows[-6:] if len(annual_rows) > 6 else annual_rows
    for ann in recent_annual:
        fy_end = ann["fy_end"]
        fy_start = ann.get("fy_start")
        d_fy_start = _parse_date(fy_start)
        d_fy_end = _parse_date(fy_end)
        if d_fy_start is None or d_fy_end is None:
            continue

        # Find Q3 YTD (240-300 days) within the same fiscal year
        def find_q3_ytd(ytd_map: dict[str, FactRecord]) -> FactRecord | None:
            for rec in ytd_map.values():
                d_s = _parse_date(rec.start)
                d_e = _parse_date(rec.end)
                if d_s is None or d_e is None:
                    continue
                if abs((d_s - d_fy_start).days) <= 12 and 240 <= (d_e - d_s).days <= 305:
                    return rec
            return None

        q3_rev = find_q3_ytd(ytd_rev)
        q3_ni = find_q3_ytd(ytd_ni)

        def _derive_q4_rev(ann_val: float | None, q3_rec: FactRecord | None) -> float | None:
            if ann_val is None or q3_rec is None:
                return None
            q4_val = ann_val - q3_rec.val
            if ann_val > 0 and (q3_rec.val <= 0 or q4_val > ann_val * 0.70):
                # Reject derived Q4 when Q3 YTD is zero/negative or <30% of annual (10-Q vs 10-K tag mismatch, e.g. CBSH/OWL/ZION)
                return None
            if q3_rec.val > 0 and (q4_val <= 0 or q4_val < 0.15 * (q3_rec.val / 3.0)):
                # Discontinued-ops restatement mismatch: compare against contemporaneous 10-K on Q3's basis
                orig_r = orig_ann_rev_by_end_tag.get((fy_end, q3_rec.tag)) or orig_ann_rev_by_end.get(fy_end)
                if orig_r is not None and abs(orig_r.val - ann_val) > abs(ann_val) * 0.05:
                    alt_q4 = orig_r.val - q3_rec.val
                    if alt_q4 > 0.25 * (q3_rec.val / 3.0):
                        return alt_q4
                if q4_val <= 0:
                    return None
            return q4_val

        if fy_end not in quarters and (q3_rev is not None or q3_ni is not None):
            q3_op = find_q3_ytd(ytd_op)
            q3_pt = find_q3_ytd(ytd_pretax)
            q3_ep = find_q3_ytd(ytd_eps)
            q4_eps = None
            if ann.get("eps_diluted") is not None and q3_ep is not None:
                sf = _cumulative_split_factor(q3_ep.filed, q_split_events)
                q4_eps = round(ann["eps_diluted"] - (q3_ep.val / sf), 4)

            quarters[fy_end] = {
                "period_end": fy_end,
                "period_start": q3_rev.end if q3_rev else (q3_ni.end if q3_ni else None),
                "fp": "Q4",
                "revenue": _derive_q4_rev(ann.get("revenue"), q3_rev),
                "operating_income": (ann["operating_income"] - q3_op.val) if (ann.get("operating_income") is not None and q3_op is not None) else None,
                "pretax_income": (ann["pretax_income"] - q3_pt.val) if (ann.get("pretax_income") is not None and q3_pt is not None) else None,
                "net_income": (ann["net_income"] - q3_ni.val) if (ann.get("net_income") is not None and q3_ni is not None) else None,
                "eps_diluted": q4_eps,
                "operating_cf": None,
                "fcf": None,
            }
        elif fy_end in quarters:
            quarters[fy_end]["fp"] = "Q4"
            # Guard against mis-tagged 10-K full-year revenue given a 90-day Q4 duration (e.g. DINO, NI, LHX)
            q4_rec = q_rev.get(fy_end)
            if (
                q4_rec is not None
                and q4_rec.form in ANNUAL_FORMS
                and q3_rev is not None
                and q3_rev.val > 0
            ):
                cur_q4_rev = quarters[fy_end].get("revenue")
                ann_rev = ann.get("revenue")
                if (
                    cur_q4_rev is not None
                    and ann_rev is not None
                    and ann_rev > 0
                    and abs(cur_q4_rev - ann_rev) / ann_rev < 0.05
                ):
                    quarters[fy_end]["revenue"] = _derive_q4_rev(ann_rev, q3_rev)
                elif (
                    cur_q4_rev is not None
                    and ann_rev is None
                    and cur_q4_rev > q3_rev.val * 1.1
                ):
                    # 10-K tagged full-year revenue with 90-day Q4 dates (e.g. LHX FY2025): derive Q4 from it
                    quarters[fy_end]["revenue"] = _derive_q4_rev(cur_q4_rev, q3_rev)

    # Compute standalone quarterly OCF/FCF by differencing ascending YTD entries sharing the same start date
    def assign_quarterly_cf(ytd_map: dict[str, FactRecord], ann_field: str, abs_outflow: bool = False) -> dict[str, float]:
        out: dict[str, float] = {}
        # Store (end, start, val, tag, is_annual) grouped by fiscal-year start month
        by_start: dict[str, list[tuple[str, str, float, str | None, bool]]] = defaultdict(list)
        for rec in ytd_map.values():
            if not rec.start:
                continue
            val = abs(rec.val) if abs_outflow else rec.val
            by_start[rec.start[:7]].append((rec.end, rec.start, val, rec.tag, False))

        # Include annual rows in the matching start-date group
        for ann in recent_annual:
            if ann.get("fy_start") and ann.get(ann_field) is not None:
                by_start[ann["fy_start"][:7]].append(
                    (ann["fy_end"], ann["fy_start"], float(ann[ann_field]), None, True)
                )

        for _start_ym, pts in by_start.items():
            # Deduplicate in ascending end-date order (prefer 10-Q YTD over annual if same end)
            by_end_item: dict[str, tuple[str, float, str | None, bool]] = {}
            for e, s, v, tag, is_ann in pts:
                if e not in by_end_item or is_ann:
                    by_end_item[e] = (s, v, tag, is_ann)
            dedup = sorted(by_end_item.items())

            prev_val: float | None = None
            prev_end: date | None = None
            prev_tag: str | None = None
            for end_str, (start_str, cum_val, tag, is_ann) in dedup:
                d_e = _parse_date(end_str)
                d_s = _parse_date(start_str)
                if d_e is None:
                    continue
                if prev_end is None:
                    # Only assign as standalone Q1 if duration is genuinely ~1 quarter (<= 125 days)
                    # Prevents dumping 6M YTD into Q2 when Q1 is absent (e.g. TAP 2026-06-30)
                    dur = (d_e - d_s).days if d_s is not None else None
                    if dur is not None and 60 <= dur <= 125:
                        out[end_str] = cum_val
                else:
                    gap = (d_e - prev_end).days
                    # Require matching XBRL tags across 10-Q YTDs (prevents DAL Q2 tag-switch negative CapEx)
                    tags_compatible = is_ann or prev_tag is None or tag is None or tag == prev_tag
                    if 60 <= gap <= 125 and tags_compatible and prev_val is not None:
                        diff_val = cum_val - prev_val
                        if not (abs_outflow and diff_val < 0):
                            out[end_str] = diff_val
                prev_val = cum_val
                prev_end = d_e
                prev_tag = tag
        return out

    q_ocf_map = assign_quarterly_cf(ytd_ocf, "operating_cf", abs_outflow=False)
    q_capex_map = assign_quarterly_cf(ytd_capex, "capex", abs_outflow=True)

    for end, q in quarters.items():
        if end in q_ocf_map:
            q["operating_cf"] = q_ocf_map[end]
            if end in q_capex_map:
                q["fcf"] = q_ocf_map[end] - q_capex_map[end]

    # Deduplicate entries separated by < 50 days and return the latest max_quarters quarters
    deduped: list[dict[str, Any]] = []
    for end in sorted(quarters):
        d_cur = _parse_date(end)
        if d_cur is None:
            continue
        if deduped:
            d_prev = _parse_date(deduped[-1]["period_end"])
            if d_prev is not None and (d_cur - d_prev).days < 50:
                if quarters[end]["revenue"] is not None and deduped[-1]["revenue"] is None:
                    deduped[-1] = quarters[end]
                continue
        deduped.append(quarters[end])

    return deduped[-max_quarters:]
