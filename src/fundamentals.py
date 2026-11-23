"""SEC EDGAR XBRL companyfacts financial/dividend extraction and split adjustment.

Key features:
- Industry-aware US-GAAP / IFRS tag priority mapping (corporates, banks, insurers, energy, etc.)
- Chained split-factor inference from overlapping 10-K comparative years (100% SEC; no Yahoo Finance)
- Per-share (EPS / DPS) and share-count normalization to latest share basis by filing date
- Annual (financials_annual), standalone quarterly (financials_quarterly), and TTM extraction
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from src.edgar import EdgarClient, normalize_cik
from src.env import CONFIG_DIR

CIK_OVERRIDES_PATH = CONFIG_DIR / "cik_overrides.json"


def load_cik_overrides(path: Path = CIK_OVERRIDES_PATH) -> dict[str, dict[str, Any]]:
    """Load curated CIK overrides and legacy predecessor CIK chains from config/cik_overrides.json."""
    if not path.is_file():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, dict[str, Any]] = {}
    for sec_id, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        cik_val = entry.get("cik")
        legacy_vals = entry.get("legacy_ciks") or []
        out[str(sec_id).strip().upper()] = {
            "cik": normalize_cik(cik_val) if cik_val else None,
            "legacy_ciks": [normalize_cik(c) for c in legacy_vals if c],
        }
    return out


def get_cik_chain(
    security_id: str,
    primary_cik: str | None,
    overrides: dict[str, dict[str, Any]],
) -> tuple[str, ...]:
    """Return an ordered, deduplicated tuple of CIKs (oldest legacy -> current) for a security."""
    entry = overrides.get(security_id.upper()) or {}
    chain: list[str] = []
    for lc in entry.get("legacy_ciks") or []:
        if lc not in chain:
            chain.append(lc)
    eff_primary = normalize_cik(primary_cik) if primary_cik else entry.get("cik")
    if eff_primary and eff_primary not in chain:
        chain.append(eff_primary)
    elif entry.get("cik") and entry["cik"] not in chain:
        chain.append(entry["cik"])
    return tuple(chain)


def merge_company_facts(facts_list: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge multiple SEC companyfacts JSON payloads (oldest -> newest) into a unified payload."""
    valid = [f for f in facts_list if isinstance(f, dict) and f.get("facts")]
    if not valid:
        return {}
    if len(valid) == 1:
        return valid[0]

    newest = valid[-1]
    merged_facts: dict[str, dict[str, dict[str, Any]]] = {}

    for payload in valid:
        for taxonomy, tags in (payload.get("facts") or {}).items():
            if not isinstance(tags, dict):
                continue
            tax_dst = merged_facts.setdefault(taxonomy, {})
            for tag, tag_obj in tags.items():
                if not isinstance(tag_obj, dict):
                    continue
                tag_dst = tax_dst.setdefault(
                    tag,
                    {
                        "label": tag_obj.get("label"),
                        "description": tag_obj.get("description"),
                        "units": {},
                    },
                )
                units_dst = tag_dst["units"]
                for unit, records in (tag_obj.get("units") or {}).items():
                    if not isinstance(records, list):
                        continue
                    unit_dst = units_dst.setdefault(unit, [])
                    seen = {
                        (
                            r.get("accn"),
                            r.get("form"),
                            r.get("start"),
                            r.get("end"),
                            r.get("filed"),
                            r.get("val"),
                        )
                        for r in unit_dst
                    }
                    for rec in records:
                        if not isinstance(rec, dict):
                            continue
                        key = (
                            rec.get("accn"),
                            rec.get("form"),
                            rec.get("start"),
                            rec.get("end"),
                            rec.get("filed"),
                            rec.get("val"),
                        )
                        if key not in seen:
                            seen.add(key)
                            unit_dst.append(rec)

    return {
        "cik": newest.get("cik"),
        "entityName": newest.get("entityName"),
        "facts": merged_facts,
    }

ANNUAL_FORMS = {"10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"}
QUARTERLY_FORMS = {"10-Q", "10-Q/A"}

# Common stock split ratios (snap to exact ratio if within +/-2.5%)
KNOWN_SPLIT_RATIOS = (
    20.0,
    15.0,
    10.0,
    8.0,
    7.0,
    6.0,
    5.0,
    4.0,
    3.0,
    2.5,
    2.0,
    1.5,
    4.0 / 3.0,
    1.25,
    1.2,
    1.1,
    0.5,
    1.0 / 3.0,
    0.25,
    0.2,
    0.1,
)

# =============================================================================
# Tag priority definitions (ordered lists of (taxonomy, tag))
# =============================================================================
TAG_MAP_FLOW_USD: dict[str, list[tuple[str, str]]] = {
    "revenue": [
        ("us-gaap", "Revenues"),
        ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"),
        ("us-gaap", "SalesRevenueNet"),
        ("us-gaap", "RevenuesNetOfInterestExpense"),
        ("us-gaap", "RevenueFromContractWithCustomerIncludingAssessedTax"),
        ("us-gaap", "SalesRevenueGoodsNet"),
        ("us-gaap", "SalesRevenueServicesNet"),
        ("us-gaap", "RegulatedAndUnregulatedOperatingRevenue"),
        ("us-gaap", "OilAndGasRevenue"),
        ("us-gaap", "RefiningAndMarketingRevenue"),
        ("us-gaap", "HealthCareOrganizationRevenue"),
        ("ifrs-full", "Revenue"),
    ],
    "gross_profit": [
        ("us-gaap", "GrossProfit"),
        ("ifrs-full", "GrossProfit"),
    ],
    "cost_of_revenue": [
        ("us-gaap", "CostOfGoodsAndServicesSold"),
        ("us-gaap", "CostOfRevenue"),
        ("us-gaap", "CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization"),
        ("us-gaap", "CostOfPurchasedOilAndGas"),
        ("ifrs-full", "CostOfSales"),
    ],
    "cost_of_goods": [
        ("us-gaap", "CostOfGoodsSold"),
    ],
    "cost_of_services": [
        ("us-gaap", "CostOfServices"),
    ],
    "operating_income": [
        ("us-gaap", "OperatingIncomeLoss"),
        ("ifrs-full", "ProfitLossFromOperatingActivities"),
    ],
    "pretax_income": [
        ("us-gaap", "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest"),
        ("us-gaap", "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"),
        ("ifrs-full", "ProfitLossBeforeTax"),
    ],
    "net_income": [
        ("us-gaap", "NetIncomeLoss"),
        ("us-gaap", "NetIncomeLossAvailableToCommonStockholdersBasic"),
        ("us-gaap", "ProfitLoss"),
        ("ifrs-full", "ProfitLossAttributableToOwnersOfParent"),
        ("ifrs-full", "ProfitLoss"),
    ],
    "rd_expense": [
        ("us-gaap", "ResearchAndDevelopmentExpense"),
        ("us-gaap", "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost"),
        ("ifrs-full", "ResearchAndDevelopmentExpense"),
    ],
    "sga_expense": [
        ("us-gaap", "SellingGeneralAndAdministrativeExpense"),
    ],
    "interest_expense": [
        ("us-gaap", "InterestExpense"),
        ("us-gaap", "InterestExpenseDebt"),
        ("us-gaap", "InterestExpenseNonoperating"),
        ("ifrs-full", "FinanceCosts"),
    ],
    "income_tax": [
        ("us-gaap", "IncomeTaxExpenseBenefit"),
        ("ifrs-full", "IncomeTaxExpenseContinuingOperations"),
    ],
    "sbc": [
        ("us-gaap", "ShareBasedCompensation"),
        ("us-gaap", "AllocatedShareBasedCompensationExpense"),
    ],
    "da_expense": [
        ("us-gaap", "DepreciationDepletionAndAmortization"),
        ("us-gaap", "DepreciationAmortizationAndAccretionNet"),
        ("us-gaap", "DepreciationAndAmortization"),
        ("us-gaap", "Depreciation"),
    ],
    "operating_cf": [
        ("us-gaap", "NetCashProvidedByUsedInOperatingActivities"),
        ("us-gaap", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"),
        ("ifrs-full", "CashFlowsFromUsedInOperatingActivities"),
    ],
    "investing_cf": [
        ("us-gaap", "NetCashProvidedByUsedInInvestingActivities"),
        ("us-gaap", "NetCashProvidedByUsedInInvestingActivitiesContinuingOperations"),
        ("ifrs-full", "CashFlowsFromUsedInInvestingActivities"),
    ],
    "financing_cf": [
        ("us-gaap", "NetCashProvidedByUsedInFinancingActivities"),
        ("us-gaap", "NetCashProvidedByUsedInFinancingActivitiesContinuingOperations"),
        ("ifrs-full", "CashFlowsFromUsedInFinancingActivities"),
    ],
    "capex": [
        ("us-gaap", "PaymentsToAcquirePropertyPlantAndEquipment"),
        ("us-gaap", "PaymentsToAcquireProductiveAssets"),
        ("us-gaap", "PaymentsToAcquireOtherPropertyPlantAndEquipment"),
        ("us-gaap", "PaymentsToAcquireOtherProductiveAssets"),
        ("us-gaap", "PaymentsToAcquireOilAndGasPropertyAndEquipment"),
        ("us-gaap", "PaymentsToAcquireMachineryAndEquipment"),
        ("us-gaap", "PaymentsForCapitalImprovements"),
        ("us-gaap", "SegmentExpenditureAdditionToLongLivedAssets"),
        ("ifrs-full", "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities"),
    ],
    "dividends_paid": [
        ("us-gaap", "PaymentsOfDividendsCommonStock"),
        ("us-gaap", "PaymentsOfDividends"),
        ("us-gaap", "PaymentsOfOrdinaryDividends"),
        ("us-gaap", "DividendsCommonStockCash"),
        ("us-gaap", "DividendsCash"),
        ("us-gaap", "PaymentsOfCapitalDistribution"),
        ("us-gaap", "PartnersCapitalAccountDistributions"),
        ("ifrs-full", "DividendsPaidClassifiedAsFinancingActivities"),
    ],
    "buybacks": [
        ("us-gaap", "PaymentsForRepurchaseOfCommonStock"),
        ("us-gaap", "PaymentsForRepurchaseOfEquity"),
        ("us-gaap", "StockRepurchasedDuringPeriodValue"),
        ("ifrs-full", "PaymentsToAcquireOrRedeemEntitysShares"),
    ],
}

# Bank revenue fallback (net interest income + noninterest income)
BANK_NII_TAGS = [("us-gaap", "InterestIncomeExpenseNet")]
BANK_NONINT_TAGS = [("us-gaap", "NoninterestIncome")]
# REIT lease revenue fallback (ASC 842 rental income excluded from ASC 606 contract revenue)
REIT_LEASE_TAGS = [
    ("us-gaap", "OperatingLeaseLeaseIncome"),
    ("us-gaap", "RealEstateRevenueNet"),
]

TAG_MAP_FLOW_PER_SHARE: dict[str, list[tuple[str, str]]] = {
    "eps_diluted": [
        ("us-gaap", "EarningsPerShareDiluted"),
        ("us-gaap", "EarningsPerShareBasicAndDiluted"),
        ("us-gaap", "IncomeLossFromContinuingOperationsPerDilutedShare"),
        ("ifrs-full", "DilutedEarningsLossPerShare"),
    ],
    "dps_declared": [
        ("us-gaap", "CommonStockDividendsPerShareDeclared"),
    ],
    "dps_paid": [
        ("us-gaap", "CommonStockDividendsPerShareCashPaid"),
    ],
}

TAG_MAP_FLOW_SHARES: dict[str, list[tuple[str, str]]] = {
    "shares_diluted": [
        ("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding"),
        ("us-gaap", "WeightedAverageNumberOfShareOutstandingBasicAndDiluted"),
        ("us-gaap", "WeightedAverageNumberOfSharesOutstandingBasic"),
        ("ifrs-full", "AdjustedWeightedAverageShares"),
    ],
}

TAG_MAP_INSTANT_USD: dict[str, list[tuple[str, str]]] = {
    "total_assets": [
        ("us-gaap", "Assets"),
        ("ifrs-full", "Assets"),
    ],
    "current_assets": [
        ("us-gaap", "AssetsCurrent"),
        ("ifrs-full", "CurrentAssets"),
    ],
    "total_equity": [
        ("us-gaap", "StockholdersEquity"),
        ("us-gaap", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"),
        ("ifrs-full", "EquityAttributableToOwnersOfParent"),
        ("ifrs-full", "Equity"),
    ],
    "current_liabilities": [
        ("us-gaap", "LiabilitiesCurrent"),
        ("ifrs-full", "CurrentLiabilities"),
    ],
    "goodwill": [
        ("us-gaap", "Goodwill"),
        ("ifrs-full", "Goodwill"),
    ],
    "intangibles": [
        ("us-gaap", "IntangibleAssetsNetExcludingGoodwill"),
        ("us-gaap", "FiniteLivedIntangibleAssetsNet"),
        ("ifrs-full", "IntangibleAssetsOtherThanGoodwill"),
    ],
    "cash": [
        ("us-gaap", "CashAndCashEquivalentsAtCarryingValue"),
        ("us-gaap", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"),
        ("us-gaap", "CashAndDueFromBanks"),
        ("us-gaap", "Cash"),
        ("ifrs-full", "CashAndCashEquivalents"),
    ],
    # Prefer combined total-debt tags; otherwise sum long-term and short-term debt
    "debt_combined": [
        ("us-gaap", "DebtAndCapitalLeaseObligations"),
        ("us-gaap", "DebtLongtermAndShorttermCombinedAmount"),
    ],
    "debt_long_term": [
        ("us-gaap", "LongTermDebtNoncurrent"),
        ("us-gaap", "LongTermDebtAndCapitalLeaseObligations"),
        ("us-gaap", "LongTermDebt"),
        ("us-gaap", "LongTermNotesPayable"),
    ],
    "debt_current": [
        ("us-gaap", "DebtCurrent"),
        ("us-gaap", "LongTermDebtCurrent"),
        ("us-gaap", "ShortTermBorrowings"),
        ("us-gaap", "LongTermDebtAndCapitalLeaseObligationsCurrent"),
    ],
    "debt_short_extra": [
        ("us-gaap", "ShortTermBorrowings"),
        ("us-gaap", "CommercialPaper"),
    ],
}

TAG_MAP_INSTANT_SHARES: dict[str, list[tuple[str, str]]] = {
    "shares_outstanding": [
        ("us-gaap", "CommonStockSharesOutstanding"),
        ("dei", "EntityCommonStockSharesOutstanding"),
    ],
}


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


def _snap_split_ratio(ratio: float) -> float:
    """Snap an empirical split ratio to the exact theoretical ratio when sufficiently close."""
    for known in KNOWN_SPLIT_RATIOS:
        if abs(ratio / known - 1.0) <= 0.025:
            return known
    return round(ratio, 4)


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


def _cumulative_split_factor(filed: str | None, split_events: list[tuple[str, float]]) -> float:
    """Return the cumulative split factor from the given filing date to the latest filing."""
    if not filed or not split_events:
        return 1.0
    factor = 1.0
    for effective_filed, ratio in split_events:
        if filed < effective_filed:
            factor *= ratio
    return factor


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


def extract_annual_financials(
    company_facts: dict[str, Any],
    min_year: int = 2009,
) -> tuple[list[dict[str, Any]], list[tuple[str, float]]]:
    """Extract split-adjusted annual financials and detected split events from companyfacts JSON."""
    flow_usd_raw = {
        metric: _iter_tag_records(company_facts, tags, ("USD",), ANNUAL_FORMS)
        for metric, tags in TAG_MAP_FLOW_USD.items()
    }
    flow_ps_raw = {
        metric: _iter_tag_records(company_facts, tags, ("USD/shares",), ANNUAL_FORMS)
        for metric, tags in TAG_MAP_FLOW_PER_SHARE.items()
    }
    flow_sh_raw = {
        metric: _iter_tag_records(company_facts, tags, ("shares",), ANNUAL_FORMS)
        for metric, tags in TAG_MAP_FLOW_SHARES.items()
    }

    shares_out_bs_raw = _iter_tag_records(
        company_facts,
        [("us-gaap", "CommonStockSharesOutstanding")],
        ("shares",),
        ANNUAL_FORMS,
    )

    # 1. Infer stock split events (using both diluted shares and period-end CommonStockSharesOutstanding)
    split_events = _estimate_split_events(
        shares_recs=flow_sh_raw["shares_diluted"],
        eps_recs=flow_ps_raw["eps_diluted"],
        dps_decl_recs=flow_ps_raw["dps_declared"],
        dps_paid_recs=flow_ps_raw["dps_paid"],
        shares_out_recs=shares_out_bs_raw,
    )

    # 2. Pick the best annual record per fy_end for each flow metric
    flow_usd = {m: _select_best_annual_flow(recs) for m, recs in flow_usd_raw.items()}
    flow_ps = {
        m: _select_best_annual_flow(recs, avoid_integer_rounding=True)
        for m, recs in flow_ps_raw.items()
    }
    flow_sh = {m: _select_best_annual_flow(recs) for m, recs in flow_sh_raw.items()}

    # Guard against partial/segment us-gaap:Revenues leaks where ASC 606 revenue is the true consolidated total
    # (e.g. GIS 2023-2024, BLK 2022-2024, OWL 2022-2025) while preserving E&P hedge-loss years
    rev_by_end_tag: dict[tuple[str, str], FactRecord] = {}
    for rec in flow_usd_raw["revenue"]:
        if not _is_annual_flow(rec):
            continue
        k = (rec.end, rec.tag)
        prev_r = rev_by_end_tag.get(k)
        if prev_r is None or _rec_recency_key(rec) > _rec_recency_key(prev_r):
            rev_by_end_tag[k] = rec

    both_rev_ends = [
        fe
        for fe in flow_usd["revenue"]
        if (fe, "us-gaap:Revenues") in rev_by_end_tag
        and (fe, "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax") in rev_by_end_tag
    ]
    all_plain_lt_asc = bool(both_rev_ends) and all(
        rev_by_end_tag[(fe, "us-gaap:Revenues")].val
        < rev_by_end_tag[(fe, "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax")].val
        for fe in both_rev_ends
    )
    latest_rev_end = max(flow_usd["revenue"]) if flow_usd["revenue"] else None
    plain_dropped_latest = (
        latest_rev_end is not None
        and (latest_rev_end, "us-gaap:Revenues") not in rev_by_end_tag
    )
    for fe in list(flow_usd["revenue"]):
        chosen_r = flow_usd["revenue"][fe]
        if chosen_r.tag != "us-gaap:Revenues":
            continue
        asc_r = rev_by_end_tag.get(
            (fe, "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax")
        )
        if asc_r is None or asc_r.val <= 0:
            continue
        if (asc_r.filed > chosen_r.filed and asc_r.val > chosen_r.val * 1.25) or (
            chosen_r.val > 0
            and asc_r.val > chosen_r.val * 1.55
            and (all_plain_lt_asc or plain_dropped_latest)
        ):
            flow_usd["revenue"][fe] = asc_r

    # Bank revenue synthesis (net interest income + noninterest income):
    # apply both when revenue tags are absent AND when ASC 606 RevenueFromContractWithCustomer*
    # captures only non-interest fee income (banks exclude interest income from ASC 606).
    bank_nii = _select_best_annual_flow(
        _iter_tag_records(company_facts, BANK_NII_TAGS, ("USD",), ANNUAL_FORMS)
    )
    bank_nonint = _select_best_annual_flow(
        _iter_tag_records(company_facts, BANK_NONINT_TAGS, ("USD",), ANNUAL_FORMS)
    )
    for fy_end in set(bank_nii) & set(bank_nonint):
        r1 = bank_nii[fy_end]
        r2 = bank_nonint[fy_end]
        bank_tot = r1.val + r2.val
        cur_rev = flow_usd["revenue"].get(fy_end)
        use_bank = cur_rev is None or (
            "RevenueFromContractWithCustomer" in cur_rev.tag
            and r1.val > 0
            and bank_tot > max(0.0, cur_rev.val) * 1.5
        )
        if use_bank:
            flow_usd["revenue"][fy_end] = FactRecord(
                val=bank_tot,
                end=fy_end,
                start=r1.start or r2.start,
                filed=max(r1.filed, r2.filed),
                form=r1.form,
                accn=r1.accn,
                tag="synthetic:BankNII+Noninterest",
                priority=99,
            )

    # REIT lease revenue fallback (ASC 842 rental income excluded from ASC 606 contract revenue, e.g. CPT)
    reit_lease = _select_best_annual_flow(
        _iter_tag_records(company_facts, REIT_LEASE_TAGS, ("USD",), ANNUAL_FORMS)
    )
    for fy_end, lease_r in reit_lease.items():
        if lease_r.val <= 0:
            continue
        cur_rev = flow_usd["revenue"].get(fy_end)
        if cur_rev is None:
            flow_usd["revenue"][fy_end] = lease_r
        elif (
            "RevenueFromContractWithCustomer" in cur_rev.tag
            and lease_r.val > max(0.0, cur_rev.val) * 2.0
        ):
            combined_val = lease_r.val + max(0.0, cur_rev.val)
            flow_usd["revenue"][fy_end] = FactRecord(
                val=combined_val,
                end=fy_end,
                start=lease_r.start,
                filed=max(lease_r.filed, cur_rev.filed),
                form=lease_r.form,
                accn=lease_r.accn,
                tag="synthetic:Lease+ASC606",
                priority=98,
            )

    # Build the valid fy_end set from core PL/CF metrics (revenue / net_income / operating_cf)
    core_ends = sorted(
        {
            end
            for end in (
                set(flow_usd["revenue"])
                | set(flow_usd["net_income"])
                | set(flow_usd["operating_cf"])
            )
            if int(end[:4]) >= min_year
        }
    )

    # Deduplicate fy_end dates separated by a few days (e.g. 52/53-week calendar amendments or comparative typos)
    # while keeping normal annual period ends (>= 200 days apart)
    filtered_ends: list[str] = []
    for end in core_ends:
        d_cur = _parse_date(end)
        if d_cur is None:
            continue
        if filtered_ends:
            d_prev = _parse_date(filtered_ends[-1])
            if d_prev is not None and (d_cur - d_prev).days < 200:
                # Keep the record with more populated fields
                prev_end = filtered_ends[-1]
                prev_score = sum(
                    1
                    for m in ("revenue", "net_income", "operating_cf")
                    if prev_end in flow_usd[m]
                )
                cur_score = sum(
                    1
                    for m in ("revenue", "net_income", "operating_cf")
                    if end in flow_usd[m]
                )
                if cur_score > prev_score:
                    filtered_ends[-1] = end
                continue
        filtered_ends.append(end)

    # 3. Align balance sheet and period-end share counts (instant facts) to target fy_ends
    instant_usd = {
        metric: _select_best_instant_for_dates(
            _iter_tag_records(company_facts, TAG_MAP_INSTANT_USD[metric], ("USD",), ANNUAL_FORMS),
            filtered_ends,
        )
        for metric in (
            "total_assets",
            "current_assets",
            "total_equity",
            "current_liabilities",
            "cash",
            "goodwill",
            "intangibles",
        )
    }
    total_debt_map = _extract_total_debt(company_facts, filtered_ends)

    # Period-end shares: prefer CommonStockSharesOutstanding (+/-15d), else EntityCommonStockSharesOutstanding (up to +75d)
    shares_out_bs = _select_best_instant_for_dates(
        shares_out_bs_raw,
        filtered_ends,
        max_day_diff_before=15,
        max_day_diff_after=15,
    )
    shares_out_dei = _select_best_instant_for_dates(
        _iter_tag_records(
            company_facts,
            [("dei", "EntityCommonStockSharesOutstanding")],
            ("shares",),
            ANNUAL_FORMS,
        ),
        filtered_ends,
        max_day_diff_before=10,
        max_day_diff_after=75,
    )

    # Pre-collect single-quarter dividend payments across annual+quarterly forms as a fallback
    # for issuers that initiated dividends mid-year without tagging a full-year flow (e.g. AAPL FY2012)
    q_div_paid_all = _select_best_single_quarter(
        _iter_tag_records(
            company_facts,
            TAG_MAP_FLOW_USD["dividends_paid"],
            ("USD",),
            ANNUAL_FORMS | QUARTERLY_FORMS,
        )
    )

    # 4. Assemble annual rows and apply split adjustments
    rows: list[dict[str, Any]] = []
    for fy_end in filtered_ends:
        rev_rec = flow_usd["revenue"].get(fy_end)
        gp_rec = flow_usd["gross_profit"].get(fy_end)
        cor_rec = flow_usd["cost_of_revenue"].get(fy_end)
        cog_rec = flow_usd["cost_of_goods"].get(fy_end)
        cos_rec = flow_usd["cost_of_services"].get(fy_end)
        op_rec = flow_usd["operating_income"].get(fy_end)
        pretax_rec = flow_usd["pretax_income"].get(fy_end)
        ni_rec = flow_usd["net_income"].get(fy_end)
        rd_rec = flow_usd["rd_expense"].get(fy_end)
        sga_rec = flow_usd["sga_expense"].get(fy_end)
        int_rec = flow_usd["interest_expense"].get(fy_end)
        tax_rec = flow_usd["income_tax"].get(fy_end)
        ocf_rec = flow_usd["operating_cf"].get(fy_end)
        icf_rec = flow_usd["investing_cf"].get(fy_end)
        fcf_fin_rec = flow_usd["financing_cf"].get(fy_end)
        capex_rec = flow_usd["capex"].get(fy_end)
        sbc_rec = flow_usd["sbc"].get(fy_end)
        da_rec = flow_usd["da_expense"].get(fy_end)
        div_rec = flow_usd["dividends_paid"].get(fy_end)
        buyback_rec = flow_usd["buybacks"].get(fy_end)

        eps_rec = flow_ps["eps_diluted"].get(fy_end)
        dps_d_rec = flow_ps["dps_declared"].get(fy_end)
        dps_p_rec = flow_ps["dps_paid"].get(fy_end)
        sh_dil_rec = flow_sh["shares_diluted"].get(fy_end)
        sh_out_rec = shares_out_bs.get(fy_end) or shares_out_dei.get(fy_end)

        # Representative filing date and fiscal-year start date
        anchor_rec = ni_rec or rev_rec or ocf_rec or eps_rec
        fy_start = anchor_rec.start if anchor_rec else None
        filed = anchor_rec.filed if anchor_rec else None

        # Adjust each per-share metric and share count using the split factor for its own filing date
        eps_factor = _cumulative_split_factor(eps_rec.filed if eps_rec else filed, split_events)
        dps_d_factor = _cumulative_split_factor(dps_d_rec.filed if dps_d_rec else filed, split_events)
        dps_p_factor = _cumulative_split_factor(dps_p_rec.filed if dps_p_rec else filed, split_events)
        sh_dil_factor = _cumulative_split_factor(sh_dil_rec.filed if sh_dil_rec else filed, split_events)
        sh_out_factor = _cumulative_split_factor(sh_out_rec.filed if sh_out_rec else filed, split_events)

        eps_adj = (eps_rec.val / eps_factor) if eps_rec is not None else None
        dps_d_adj = (dps_d_rec.val / dps_d_factor) if dps_d_rec is not None else None
        dps_p_adj = (dps_p_rec.val / dps_p_factor) if dps_p_rec is not None else None
        sh_dil_adj = (sh_dil_rec.val * sh_dil_factor) if (sh_dil_rec is not None and sh_dil_rec.val > 0) else None
        sh_out_adj = (sh_out_rec.val * sh_out_factor) if (sh_out_rec is not None and sh_out_rec.val > 0) else None

        # Correct 1,000x and 1,000,000x XBRL share-count unit scale errors (e.g. shares filed in thousands/millions)
        implied_sh: float | None = None
        if (
            ni_rec is not None
            and eps_adj is not None
            and abs(eps_adj) >= 0.05
            and abs(ni_rec.val) >= 1_000_000.0
        ):
            implied_sh = abs(ni_rec.val / eps_adj)
        sh_dil_adj = _fix_share_scale(sh_dil_adj, implied_sh)
        ref_sh_for_out = sh_dil_adj or implied_sh
        sh_out_adj = _fix_share_scale(sh_out_adj, ref_sh_for_out)
        # If EPS was absent (e.g. dual-class) and sh_dil looks 1e3x/1e6x smaller than sh_out, cross-check too
        if implied_sh is None and sh_dil_adj is not None and sh_out_adj is not None and sh_out_adj > 0:
            if sh_dil_adj < 100_000.0 and sh_out_adj >= 1_000_000.0:
                sh_dil_adj = _fix_share_scale(sh_dil_adj, sh_out_adj)

        # Normalize CapEx, dividends paid, buybacks, and expense line items to positive magnitudes
        rev_val = rev_rec.val if rev_rec is not None else None
        if gp_rec is not None:
            gp_val: float | None = gp_rec.val
        elif rev_val is not None and cor_rec is not None:
            gp_val = rev_val - abs(cor_rec.val)
        elif rev_val is not None and (cog_rec is not None or cos_rec is not None):
            # Sum separate goods and services cost tags before subtracting from revenue (e.g. VZ, LMT, SLB)
            total_cost = (abs(cog_rec.val) if cog_rec is not None else 0.0) + (
                abs(cos_rec.val) if cos_rec is not None else 0.0
            )
            gp_val = rev_val - total_cost
        else:
            gp_val = None

        capex_val = abs(capex_rec.val) if capex_rec is not None else None
        ocf_val = ocf_rec.val if ocf_rec is not None else None
        fcf_val = (ocf_val - capex_val) if (ocf_val is not None and capex_val is not None) else None
        div_paid_val = abs(div_rec.val) if div_rec is not None else None
        if (
            div_paid_val is None
            and fy_start is not None
            and ((dps_d_adj is not None and dps_d_adj > 0) or (dps_p_adj is not None and dps_p_adj > 0))
        ):
            # Sum non-overlapping quarterly PaymentsOfDividends within [fy_start, fy_end] (e.g. AAPL FY2012)
            d_fys = _parse_date(fy_start)
            d_fye = _parse_date(fy_end)
            if d_fys is not None and d_fye is not None:
                in_fy = [
                    r
                    for r in q_div_paid_all.values()
                    if r.start
                    and _parse_date(r.start) is not None
                    and _parse_date(r.end) is not None
                    and (_parse_date(r.start) - d_fys).days >= -7
                    and (_parse_date(r.end) - d_fye).days <= 7
                    and abs(r.val) > 0
                ]
                if in_fy:
                    div_paid_val = sum(abs(r.val) for r in in_fy)
        buyback_val = abs(buyback_rec.val) if buyback_rec is not None else None

        # Representative split factor (based on PL filing date)
        rep_split_factor = _cumulative_split_factor(filed, split_events)

        # Reject corrupted XBRL per-share DPS facts (e.g. 1,000x/1,000,000x unit scaling errors)
        # so downstream metrics fall back cleanly to dividends_paid / shares
        denom_sh = max(sh_dil_adj or 0.0, sh_out_adj or 0.0)
        est_dps = (div_paid_val / denom_sh) if (div_paid_val is not None and denom_sh > 0) else None

        def _clean_per_share_dps(val: float | None, sf: float) -> float | None:
            if val is None:
                return None
            if val < 0:
                return None
            if sf >= 0.05 and val > 150.0:
                return None
            if est_dps is not None and est_dps > 0 and val > 25.0 and (val / est_dps) > 20.0:
                return None
            return val

        dps_d_adj = _clean_per_share_dps(dps_d_adj, dps_d_factor)
        dps_p_adj = _clean_per_share_dps(dps_p_adj, dps_p_factor)

        # Validate dps_declared against dps_paid and implied DPS (catches PLD footnote sub-item leaks,
        # ALLY 0.0 placeholders, and single-quarter values mis-tagged with annual durations)
        if dps_d_adj is not None:
            if dps_d_adj == 0.0 and (
                (dps_p_adj is not None and dps_p_adj > 0.0)
                or (est_dps is not None and est_dps > 0.05)
            ):
                dps_d_adj = None
            elif (
                dps_d_adj > 0.0
                and dps_p_adj is not None
                and dps_p_adj > 0.0
                and (dps_d_adj < dps_p_adj * 0.5 or dps_d_adj > dps_p_adj * 2.0)
            ):
                # When declared disagrees >50% with paid while implied corroborates paid, discard declared
                if est_dps is None or (0.5 * dps_p_adj <= est_dps <= 1.5 * dps_p_adj) or (
                    abs(est_dps - dps_p_adj) < abs(est_dps - dps_d_adj)
                ):
                    dps_d_adj = None
            elif (
                dps_d_adj > 0.0
                and dps_p_adj is None
                and est_dps is not None
                and est_dps > 0.20
                and 0.15 <= (dps_d_adj / est_dps) <= 0.35
            ):
                # Single-quarter DPS (~1/4 of annual implied) mis-tagged as annual
                dps_d_adj = None

        rows.append(
            {
                "fy_end": fy_end,
                "fy_start": fy_start,
                "filed": filed,
                "revenue": rev_val,
                "gross_profit": gp_val,
                "operating_income": op_rec.val if op_rec else None,
                "pretax_income": pretax_rec.val if pretax_rec else None,
                "net_income": ni_rec.val if ni_rec else None,
                "rd_expense": abs(rd_rec.val) if rd_rec else None,
                "sga_expense": abs(sga_rec.val) if sga_rec else None,
                "interest_expense": abs(int_rec.val) if int_rec else None,
                "income_tax": tax_rec.val if tax_rec else None,
                "eps_diluted": round(eps_adj, 4) if eps_adj is not None else None,
                "shares_diluted": sh_dil_adj,
                "shares_outstanding": sh_out_adj,
                "operating_cf": ocf_val,
                "investing_cf": icf_rec.val if icf_rec is not None else None,
                "financing_cf": fcf_fin_rec.val if fcf_fin_rec is not None else None,
                "capex": capex_val,
                "fcf": fcf_val,
                "sbc": abs(sbc_rec.val) if sbc_rec else None,
                "da_expense": abs(da_rec.val) if da_rec else None,
                "dividends_paid": div_paid_val,
                "buybacks": buyback_val,
                "total_assets": instant_usd["total_assets"][fy_end].val
                if (fy_end in instant_usd["total_assets"] and instant_usd["total_assets"][fy_end].val > 0)
                else None,
                "current_assets": instant_usd["current_assets"][fy_end].val
                if fy_end in instant_usd["current_assets"]
                else None,
                "total_equity": instant_usd["total_equity"][fy_end].val
                if fy_end in instant_usd["total_equity"]
                else None,
                "current_liabilities": instant_usd["current_liabilities"][fy_end].val
                if fy_end in instant_usd["current_liabilities"]
                else None,
                "cash": instant_usd["cash"][fy_end].val
                if fy_end in instant_usd["cash"]
                else None,
                "total_debt": total_debt_map.get(fy_end),
                "goodwill": instant_usd["goodwill"][fy_end].val
                if fy_end in instant_usd["goodwill"]
                else None,
                "intangibles": instant_usd["intangibles"][fy_end].val
                if fy_end in instant_usd["intangibles"]
                else None,
                "dps_declared": round(dps_d_adj, 4) if dps_d_adj is not None else None,
                "dps_paid": round(dps_p_adj, 4) if dps_p_adj is not None else None,
                "split_factor": rep_split_factor,
            }
        )

    # Cross-year median share-scale normalization (catches isolated 1,000x / 1,000,000x unit errors
    # on rows where EPS was absent, e.g. BRK.B 2009)
    for field in ("shares_diluted", "shares_outstanding"):
        vals = [r[field] for r in rows if r[field] is not None and r[field] > 0]
        if len(vals) >= 3:
            med = statistics.median(vals)
            for r in rows:
                if r[field] is not None and r[field] > 0:
                    r[field] = _fix_share_scale(r[field], med)

    return rows, split_events


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
            and (rec.priority < prev.priority or (rec.priority == prev.priority and _rec_recency_key(rec) > _rec_recency_key(prev)))
        ):
            by_end[rec.end] = rec
    return by_end


def extract_quarterly_financials(
    company_facts: dict[str, Any],
    annual_rows: list[dict[str, Any]],
    split_events: list[tuple[str, float]],
    max_quarters: int = 16,
) -> list[dict[str, Any]]:
    """Extract up to max_quarters standalone (3-month) PL/CF quarters (deriving Q4 from 10-K minus Q3 YTD)."""
    all_forms = ANNUAL_FORMS | QUARTERLY_FORMS

    q_rev = _select_best_single_quarter(
        _iter_tag_records(company_facts, TAG_MAP_FLOW_USD["revenue"], ("USD",), all_forms)
    )
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
            "RevenueFromContractWithCustomer" in cur_q.tag
            and r1.val > 0
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
    ytd_rev = _select_best_ytd_by_end(
        _iter_tag_records(company_facts, TAG_MAP_FLOW_USD["revenue"], ("USD",), QUARTERLY_FORMS)
    )
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
            "RevenueFromContractWithCustomer" in cur_y.tag
            and r1.val > 0
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

    # Build standalone quarter map
    quarters: dict[str, dict[str, Any]] = {}
    all_q_ends = sorted(set(q_rev) | set(q_ni))
    for end in all_q_ends:
        anchor = q_rev.get(end) or q_ni.get(end)
        eps_r = q_eps.get(end)
        eps_val = None
        if eps_r is not None:
            sf = _cumulative_split_factor(eps_r.filed, split_events)
            eps_val = round(eps_r.val / sf, 4)

        quarters[end] = {
            "period_end": end,
            "period_start": anchor.start if anchor else None,
            "fp": None,
            "revenue": q_rev[end].val if end in q_rev else None,
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
                sf = _cumulative_split_factor(q3_ep.filed, split_events)
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


def _fmt_billions(val: float | None) -> str:
    if val is None:
        return "-"
    return f"{val / 1e9:7.2f}B"


def _fmt_num(val: float | None) -> str:
    if val is None:
        return "-"
    return f"{val:6.2f}"


def verify_sample_tickers(tickers: list[str]) -> int:
    """Print a verification table of SEC XBRL extraction results for the given tickers."""
    client = EdgarClient()
    ticker_to_cik = client.build_ticker_to_cik_map()

    print(f"Tickers verified: {len(tickers)}")
    print("=" * 116)
    print(
        f"{'Ticker':<6} {'CIK':<10} {'Yrs':>3} {'LatestFY':<10} "
        f"{'Rev':>8} {'OpInc':>8} {'NetInc':>8} {'FCF':>8} "
        f"{'EPS':>6} {'DPS_d':>6} {'DPS_p':>6} {'DivPaid':>8} {'Splits'}"
    )
    print("-" * 116)

    missing_summary: dict[str, list[str]] = defaultdict(list)

    for raw_ticker in tickers:
        ticker = raw_ticker.upper()
        cik = ticker_to_cik.get(ticker)
        if not cik:
            print(f"{ticker:<6} {'NOT FOUND':<10}")
            continue

        facts = client.get_company_facts(cik)
        rows, splits = extract_annual_financials(facts)
        if not rows:
            print(f"{ticker:<6} {normalize_cik(cik):<10} {'0':>3} NO ANNUAL DATA")
            continue

        latest = rows[-1]
        for field in (
            "revenue",
            "operating_income",
            "net_income",
            "eps_diluted",
            "operating_cf",
            "capex",
            "fcf",
            "dividends_paid",
            "total_assets",
            "total_equity",
            "total_debt",
            "dps_declared",
            "dps_paid",
        ):
            if latest[field] is None:
                missing_summary[field].append(ticker)

        split_str = ", ".join(f"{dt}:{r:g}x" for dt, r in splits) if splits else "-"
        print(
            f"{ticker:<6} {cik:<10} {len(rows):>3} {latest['fy_end']:<10} "
            f"{_fmt_billions(latest['revenue']):>8} "
            f"{_fmt_billions(latest['operating_income']):>8} "
            f"{_fmt_billions(latest['net_income']):>8} "
            f"{_fmt_billions(latest['fcf']):>8} "
            f"{_fmt_num(latest['eps_diluted']):>6} "
            f"{_fmt_num(latest['dps_declared']):>6} "
            f"{_fmt_num(latest['dps_paid']):>6} "
            f"{_fmt_billions(latest['dividends_paid']):>8} "
            f"{split_str}"
        )

    print("=" * 116)
    print("Summary of missing fields in latest fiscal year:")
    for field, tlist in sorted(missing_summary.items()):
        print(f"  - {field:<18}: {len(tlist):>2} ticker(s) ({', '.join(tlist)})")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="SEC XBRL fundamentals extractor")
    parser.add_argument(
        "--verify",
        nargs="+",
        help="Verify extraction for space-separated tickers (e.g. KO PEP AVGO FAST)",
    )
    parser.add_argument(
        "--detail",
        help="Print full annual history for a single ticker",
    )
    args = parser.parse_args()

    if args.detail:
        client = EdgarClient()
        cik = client.build_ticker_to_cik_map().get(args.detail.upper())
        if not cik:
            print(f"CIK not found for {args.detail}", file=sys.stderr)
            return 1
        facts = client.get_company_facts(cik)
        rows, splits = extract_annual_financials(facts)
        print(f"=== {args.detail.upper()} (CIK {cik}) | Splits: {splits} ===")
        print(
            f"{'FY End':<10} {'Filed':<10} {'Rev':>8} {'OpInc':>8} {'NetInc':>8} "
            f"{'FCF':>8} {'EPS':>6} {'DPS_d':>6} {'DPS_p':>6} {'DivPaid':>8} {'Equity':>8} {'Debt':>8} {'Split':>6}"
        )
        for r in rows:
            print(
                f"{r['fy_end']:<10} {(r['filed'] or '-'):<10} "
                f"{_fmt_billions(r['revenue']):>8} "
                f"{_fmt_billions(r['operating_income']):>8} "
                f"{_fmt_billions(r['net_income']):>8} "
                f"{_fmt_billions(r['fcf']):>8} "
                f"{_fmt_num(r['eps_diluted']):>6} "
                f"{_fmt_num(r['dps_declared']):>6} "
                f"{_fmt_num(r['dps_paid']):>6} "
                f"{_fmt_billions(r['dividends_paid']):>8} "
                f"{_fmt_billions(r['total_equity']):>8} "
                f"{_fmt_billions(r['total_debt']):>8} "
                f"{r['split_factor']:>6.2f}"
            )
        return 0

    if args.verify:
        return verify_sample_tickers(args.verify)

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
