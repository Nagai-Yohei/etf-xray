"""Low-level XBRL FactRecord parsing, period selection, share-scale normalization, and debt extraction."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Any

from src.xbrl.tags import ANNUAL_FORMS, TAG_MAP_INSTANT_USD


@dataclass(frozen=True)
class FactRecord:
    """Lightweight container for a single XBRL fact record."""

    val: float
    end: str
    start: str | None
    filed: str
    form: str
    accn: str
    tag: str
    priority: int


def _parse_date(iso_str: str | None) -> date | None:
    if not iso_str or len(iso_str) < 10:
        return None
    try:
        return date.fromisoformat(iso_str[:10])
    except ValueError:
        return None


def _duration_days(start: str | None, end: str | None) -> int | None:
    d_start = _parse_date(start)
    d_end = _parse_date(end)
    if d_start is None or d_end is None:
        return None
    return (d_end - d_start).days


def _iter_tag_records(
    company_facts: dict[str, Any],
    tag_list: list[tuple[str, str]],
    unit_candidates: tuple[str, ...],
    allowed_forms: set[str],
) -> list[FactRecord]:
    """Extract matching XBRL fact records for the given tag list and form types."""
    facts_root = company_facts.get("facts") or {}
    out: list[FactRecord] = []

    for priority, (taxonomy, tag) in enumerate(tag_list):
        tax_obj = facts_root.get(taxonomy)
        if not tax_obj:
            continue
        concept = tax_obj.get(tag)
        if not concept:
            continue
        units = concept.get("units") or {}
        items: list[dict[str, Any]] = []
        for unit_key in unit_candidates:
            if unit_key in units:
                items = units[unit_key]
                break
        if not items:
            continue

        for row in items:
            form = str(row.get("form") or "")
            if form not in allowed_forms:
                continue
            val = row.get("val")
            end = row.get("end")
            filed = row.get("filed")
            if val is None or not end or not filed:
                continue
            try:
                fval = float(val)
            except (TypeError, ValueError):
                continue
            out.append(
                FactRecord(
                    val=fval,
                    end=str(end)[:10],
                    start=str(row["start"])[:10] if row.get("start") else None,
                    filed=str(filed)[:10],
                    form=form,
                    accn=str(row.get("accn") or ""),
                    tag=f"{taxonomy}:{tag}",
                    priority=priority,
                )
            )
    return out


def _is_annual_flow(rec: FactRecord) -> bool:
    days = _duration_days(rec.start, rec.end)
    return days is not None and 330 <= days <= 395


def _rec_recency_key(rec: FactRecord) -> tuple[str, int, str]:
    """Priority key for selecting the latest filing within the same period and tag (larger = newer)."""
    is_amendment = 1 if rec.form.endswith("/A") else 0
    return (rec.filed, is_amendment, rec.accn)


def _select_best_annual_flow(
    records: list[FactRecord],
    avoid_integer_rounding: bool = False,
) -> dict[str, FactRecord]:
    """Select the highest-priority, latest-filed annual flow record for each fy_end."""
    # Sort records chronologically by filing date within each (fy_end, priority)
    grouped: dict[tuple[str, int], list[FactRecord]] = defaultdict(list)
    for rec in records:
        if not _is_annual_flow(rec):
            continue
        grouped[(rec.end, rec.priority)].append(rec)

    by_end_prio: dict[tuple[str, int], FactRecord] = {}
    for key, grp in grouped.items():
        grp.sort(key=_rec_recency_key)
        chosen = grp[-1]
        # Avoid precision loss on per-share facts rounded with decimals=0 in later comparative filings (e.g. 2.70 -> 3)
        if avoid_integer_rounding and len(grp) >= 2 and chosen.val == round(chosen.val):
            for prev in reversed(grp[:-1]):
                if prev.val != round(prev.val) and round(prev.val) == chosen.val:
                    chosen = prev
                    break
        by_end_prio[key] = chosen

    # Pick the lowest priority index (highest priority tag) per fy_end
    selected: dict[str, FactRecord] = {}
    for (fy_end, _prio), rec in sorted(by_end_prio.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        if fy_end not in selected:
            selected[fy_end] = rec
    return selected


def _select_best_instant_for_dates(
    records: list[FactRecord],
    target_ends: list[str],
    max_day_diff_before: int = 15,
    max_day_diff_after: int = 15,
) -> dict[str, FactRecord]:
    """Match each target fy_end to the closest, highest-priority instant record."""
    instant_recs = [r for r in records if r.start is None]
    target_dates = [(end, _parse_date(end)) for end in target_ends]

    # Pick the closest date and latest filing for each (fy_end, priority)
    best: dict[str, tuple[int, int, tuple[str, int, str], FactRecord]] = {}

    for rec in instant_recs:
        rec_date = _parse_date(rec.end)
        if rec_date is None:
            continue
        for fy_end, t_date in target_dates:
            if t_date is None:
                continue
            delta = (rec_date - t_date).days
            if delta < -max_day_diff_before or delta > max_day_diff_after:
                continue
            abs_delta = abs(delta)
            # 1) Tag priority (lower is better) 2) Date distance (lower is better) 3) Latest filing
            cand_score = (rec.priority, abs_delta)
            prev = best.get(fy_end)
            if prev is None:
                best[fy_end] = (cand_score[0], cand_score[1], _rec_recency_key(rec), rec)
            else:
                prev_score = (prev[0], prev[1])
                if cand_score < prev_score or (
                    cand_score == prev_score and _rec_recency_key(rec) > prev[2]
                ):
                    best[fy_end] = (cand_score[0], cand_score[1], _rec_recency_key(rec), rec)

    return {fy_end: item[3] for fy_end, item in best.items()}


def _select_best_single_quarter(records: list[FactRecord]) -> dict[str, FactRecord]:
    """Select one standalone quarterly record (70-122 days, including 16-week retail Q1) per period_end."""
    by_end_prio: dict[tuple[str, int], FactRecord] = {}
    for rec in records:
        dur = _duration_days(rec.start, rec.end)
        if dur is None or not (70 <= dur <= 122):
            continue
        key = (rec.end, rec.priority)
        prev = by_end_prio.get(key)
        if prev is None or _rec_recency_key(rec) > _rec_recency_key(prev):
            by_end_prio[key] = rec

    selected: dict[str, FactRecord] = {}
    for (end, _prio), rec in sorted(by_end_prio.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        if end not in selected:
            selected[end] = rec
    return selected


def _select_best_ytd_by_end(records: list[FactRecord]) -> dict[str, FactRecord]:
    """Select the longest 10-Q YTD record (70-305 days) per period_end."""
    by_end: dict[str, FactRecord] = {}
    for rec in records:
        dur = _duration_days(rec.start, rec.end)
        if dur is None or not (70 <= dur <= 305):
            continue
        prev = by_end.get(rec.end)
        if prev is None:
            by_end[rec.end] = rec
            continue
        prev_dur = _duration_days(prev.start, prev.end) or 0
        # For the same end date, prefer longer YTD (6M/9M > 3M), then higher-priority tag and latest filing
        if (dur > prev_dur + 20) or (
            abs(dur - prev_dur) <= 20
            and (
                rec.priority < prev.priority
                or (rec.priority == prev.priority and _rec_recency_key(rec) > _rec_recency_key(prev))
            )
        ):
            by_end[rec.end] = rec
    return by_end


def _fix_share_scale(val: float | None, ref: float | None) -> float | None:
    """Normalize 1,000x or 1,000,000x XBRL share-count unit scale errors against a reference share count."""
    if val is None or val <= 0 or ref is None or ref <= 0:
        return val
    ratio = ref / val
    if 500_000.0 <= ratio <= 2_000_000.0:
        return val * 1_000_000.0
    if 500.0 <= ratio <= 2_000.0:
        return val * 1_000.0
    if 0.0005 <= ratio <= 0.002:
        return val / 1_000.0
    if 0.0000005 <= ratio <= 0.000002:
        return val / 1_000_000.0
    return val


def _extract_total_debt(
    company_facts: dict[str, Any],
    target_ends: list[str],
) -> dict[str, float]:
    """Extract total interest-bearing debt (short-term + long-term) without double-counting."""
    combined = _select_best_instant_for_dates(
        _iter_tag_records(company_facts, TAG_MAP_INSTANT_USD["debt_combined"], ("USD",), ANNUAL_FORMS),
        target_ends,
    )
    long_term = _select_best_instant_for_dates(
        _iter_tag_records(company_facts, TAG_MAP_INSTANT_USD["debt_long_term"], ("USD",), ANNUAL_FORMS),
        target_ends,
    )
    current = _select_best_instant_for_dates(
        _iter_tag_records(company_facts, TAG_MAP_INSTANT_USD["debt_current"], ("USD",), ANNUAL_FORMS),
        target_ends,
    )
    short_extra = _select_best_instant_for_dates(
        _iter_tag_records(company_facts, TAG_MAP_INSTANT_USD["debt_short_extra"], ("USD",), ANNUAL_FORMS),
        target_ends,
    )

    result: dict[str, float] = {}
    for fy_end in target_ends:
        if fy_end in combined:
            result[fy_end] = combined[fy_end].val
            continue

        lt = long_term.get(fy_end)
        cur = current.get(fy_end)
        extra = short_extra.get(fy_end)

        if lt is None and cur is None:
            continue

        total = 0.0
        if lt is not None:
            total += lt.val
        if cur is not None:
            # LongTermDebt may include current maturities; Noncurrent + Current is standard
            if lt is None or lt.tag != "us-gaap:LongTermDebt" or cur.tag != "us-gaap:LongTermDebtCurrent":
                total += cur.val
        # When using LongTermDebtCurrent, also add separate ShortTermBorrowings / CommercialPaper if reported
        if (
            cur is not None
            and cur.tag in ("us-gaap:LongTermDebtCurrent", "us-gaap:LongTermDebtAndCapitalLeaseObligationsCurrent")
            and extra is not None
            and extra.tag != cur.tag
        ):
            total += extra.val

        result[fy_end] = total
    return result
