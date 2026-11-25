"""SEC XBRL tag priority maps, form constants, and CIK override/merge helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.edgar import normalize_cik
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
    50.0,
    30.0,
    25.0,
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
        ("us-gaap", "RevenuesNetOfInterestExpense"),
        ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"),
        ("us-gaap", "SalesRevenueNet"),
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
        ("us-gaap", "PaymentsOfCapitalDistribution"),
        ("us-gaap", "PartnersCapitalAccountDistributions"),
        ("ifrs-full", "DividendsPaidClassifiedAsFinancingActivities"),
        ("us-gaap", "DividendsCommonStockCash"),
        ("us-gaap", "DividendsCash"),
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
# Common-only equity statement dividend tag and preferred dividend tags (used to exclude preferred
# dividends when cash-flow statements only report combined PaymentsOfDividends / PaymentsOfOrdinaryDividends)
COMMON_DIV_CASH_TAGS = [
    ("us-gaap", "DividendsCommonStockCash"),
]
PREFERRED_DIV_TAGS = [
    ("us-gaap", "PaymentsOfDividendsPreferredStockAndPreferenceStock"),
    ("us-gaap", "DividendsPreferredStockCash"),
    ("us-gaap", "PreferredStockDividendsIncomeStatementImpact"),
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
