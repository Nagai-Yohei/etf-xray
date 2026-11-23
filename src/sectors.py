"""Sector classification derived exclusively from official SEC EDGAR SIC codes.

Maps 4-digit SEC Standard Industrial Classification (SIC) codes to broad accounting
sector groups so downstream metrics and UI views can suppress or annotate ratios
that are structurally distorted by sector-specific accounting (e.g., REIT GAAP
depreciation, bank/insurer financial balance sheets, regulated utility CapEx).
"""

from __future__ import annotations

# Broad accounting sector groups used for metric suppression and UI annotations
SECTOR_LABELS: dict[str, str] = {
    "reit": "REIT",
    "bank": "Bank",
    "insurance": "Insurance",
    "asset_mgmt": "Financial / Asset Mgmt",
    "utility": "Utility",
    "energy_upstream": "Energy (Upstream / Integrated)",
    "telecom": "Telecom",
}

FINANCIAL_GROUPS = frozenset({"bank", "insurance", "asset_mgmt"})


def normalize_sic(raw_sic: object) -> str | None:
    """Normalize an SEC SIC code into a 4-digit numeric string, or None if invalid/empty."""
    if raw_sic is None:
        return None
    digits = "".join(ch for ch in str(raw_sic).strip() if ch.isdigit())
    if not digits:
        return None
    if len(digits) > 4:
        return None
    return digits.zfill(4)


def classify_sector_group(
    sic: str | None,
    sic_description: str | None = None,
) -> str | None:
    """Map an official SEC 4-digit SIC code to a normalized accounting sector group.

    Returns one of:
      - 'reit'
      - 'bank'
      - 'insurance'
      - 'asset_mgmt'
      - 'utility'
      - 'energy_upstream'
      - 'telecom'
      - None (standard corporate / non-specialized accounting)
    """
    code = normalize_sic(sic)
    desc = (sic_description or "").strip().upper()
    if code is None:
        return None

    num = int(code)

    # REITs (official SEC SIC 6798)
    if num == 6798 or "REAL ESTATE INVESTMENT TRUST" in desc:
        return "reit"

    # Depository banks, savings institutions, GSEs, and consumer/commercial credit lenders
    if (6020 <= num <= 6099) or num in {6111, 6141, 6153, 6159, 6162}:
        return "bank"

    # Insurance underwriters (6311-6399); deliberately exclude 6411 (fee-based insurance brokers)
    if 6311 <= num <= 6399:
        return "insurance"

    # Broker-dealers, investment advisors, asset managers, PE, BDCs, finance services
    if (6200 <= num <= 6282) or num in {6199, 6726}:
        return "asset_mgmt"

    # Regulated electric, gas distribution, water, and combination utilities
    # (exclude 4922 midstream pipelines and 4950+ waste/sanitary services)
    if num in {4911, 4923, 4924, 4931, 4932, 4939, 4941}:
        return "utility"

    # Upstream oil & gas E&P, oilfield services, and integrated oil & refining
    if num in {1311, 1381, 1382, 1389, 2911}:
        return "energy_upstream"

    # Telecommunications & cable/pay-TV infrastructure
    if num in {4812, 4813, 4841}:
        return "telecom"

    return None
