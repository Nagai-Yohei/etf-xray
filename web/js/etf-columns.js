/**
 * Column definitions, presets, and localStorage persistence for ETF View holdings table.
 */

import {
  fmtMultiple,
  fmtPct,
  fmtPerShare,
  fmtPrice,
  fmtStreak,
  fmtUsdCompact,
} from "./dom.js";

const STORAGE_KEY_COLS = "etf_xray_visible_cols_v1";

// Customizable metric column definitions
export const OPTIONAL_COLUMNS = [
  { key: "price", label: "Implied Price", group: "Valuation", get: (r) => r.price, fmt: (v) => fmtPrice(v) },
  { key: "ttm_yield", label: "TTM Yield", group: "Dividend", get: (r) => r.m?.ttm_yield, fmt: (v) => fmtPct(v, 2) },
  { key: "div_streak_years", label: "Div Streak", group: "Dividend", get: (r) => r.m?.div_streak_years, fmt: (v, r) => fmtStreak(v, r.m?.div_streak_open) },
  { key: "div_cagr_5y", label: "5Y Div CAGR", group: "Dividend", get: (r) => r.m?.div_cagr_5y, fmt: (v) => fmtPct(v, 1, true) },
  { key: "div_cagr_10y", label: "10Y Div CAGR", group: "Dividend", get: (r) => r.m?.div_cagr_10y, fmt: (v) => fmtPct(v, 1, true) },
  { key: "payout_ratio_eps", label: "Payout (EPS)", group: "Dividend", get: (r) => r.m?.payout_ratio_eps, fmt: (v) => fmtPct(v, 1) },
  { key: "payout_ratio_fcf", label: "Payout (FCF)", group: "Dividend", get: (r) => r.m?.payout_ratio_fcf, fmt: (v) => fmtPct(v, 1) },
  { key: "dps_ttm", label: "TTM DPS", group: "Dividend", get: (r) => r.m?.dps_ttm, fmt: (v) => fmtPerShare(v) },
  { key: "pe_ratio", label: "P/E (TTM)", group: "Valuation", get: (r) => r.m?.pe_ratio, fmt: (v) => fmtMultiple(v) },
  { key: "pb_ratio", label: "P/B", group: "Valuation", get: (r) => r.m?.pb_ratio, fmt: (v) => fmtMultiple(v) },
  { key: "revenue_ttm", label: "TTM Rev", group: "Fundamentals", get: (r) => r.m?.revenue_ttm, fmt: (v) => fmtUsdCompact(v) },
  { key: "rev_cagr_5y", label: "5Y Rev CAGR", group: "Fundamentals", get: (r) => r.m?.rev_cagr_5y, fmt: (v) => fmtPct(v, 1, true) },
  { key: "gross_margin", label: "Gross Margin", group: "Fundamentals", get: (r) => r.m?.gross_margin, fmt: (v) => fmtPct(v, 1) },
  { key: "op_margin", label: "Op Margin", group: "Fundamentals", get: (r) => r.m?.op_margin, fmt: (v) => fmtPct(v, 1) },
  { key: "fcf_margin", label: "FCF Margin", group: "Fundamentals", get: (r) => r.m?.fcf_margin, fmt: (v) => fmtPct(v, 1) },
  { key: "fcf_ex_sbc_margin", label: "Adj. FCF Mgn", group: "Fundamentals", get: (r) => r.m?.fcf_ex_sbc_margin, fmt: (v) => fmtPct(v, 1) },
  { key: "fcf_cagr_5y", label: "5Y FCF CAGR", group: "Fundamentals", get: (r) => r.m?.fcf_cagr_5y, fmt: (v) => fmtPct(v, 1, true) },
  { key: "fcf_positive_years", label: "FCF +Yrs", group: "Fundamentals", get: (r) => r.m?.fcf_positive_years, fmt: (v) => (v !== null && v !== undefined ? `${v} yrs` : "—") },
  { key: "roic", label: "ROIC", group: "Fundamentals", get: (r) => r.m?.roic, fmt: (v) => fmtPct(v, 1) },
  { key: "roe", label: "ROE", group: "Fundamentals", get: (r) => r.m?.roe, fmt: (v) => fmtPct(v, 1) },
  { key: "roe_5y_median", label: "5Y Med ROE", group: "Fundamentals", get: (r) => r.m?.roe_5y_median, fmt: (v) => fmtPct(v, 1) },
  { key: "equity_ratio", label: "Equity Ratio", group: "Fundamentals", get: (r) => r.m?.equity_ratio, fmt: (v) => fmtPct(v, 1) },
  { key: "share_change_5y", label: "5Y Share Δ", group: "Fundamentals", get: (r) => r.m?.share_change_5y, fmt: (v) => fmtPct(v, 1, true) },
  { key: "interest_coverage", label: "Int. Cov.", group: "Fundamentals", get: (r) => r.m?.interest_coverage, fmt: (v) => fmtMultiple(v) },
];

export const COLUMN_PRESETS = {
  Overview: ["price", "ttm_yield", "div_streak_years", "div_cagr_5y", "payout_ratio_eps", "pe_ratio", "roic"],
  Dividend: ["ttm_yield", "div_streak_years", "div_cagr_5y", "div_cagr_10y", "payout_ratio_eps", "payout_ratio_fcf", "dps_ttm"],
  Fundamentals: ["revenue_ttm", "gross_margin", "op_margin", "fcf_ex_sbc_margin", "roic", "roe", "share_change_5y"],
  Valuation: ["price", "pe_ratio", "pb_ratio", "ttm_yield", "dps_ttm"],
  All: OPTIONAL_COLUMNS.map((c) => c.key),
};

export function loadSavedColumns() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY_COLS);
    if (raw) {
      const parsed = JSON.parse(raw);
      if (Array.isArray(parsed) && parsed.length > 0) {
        const valid = new Set(OPTIONAL_COLUMNS.map((c) => c.key));
        const filtered = parsed.filter((k) => valid.has(k));
        if (filtered.length > 0) return new Set(filtered);
      }
    }
  } catch {
    // Fall back to default preset when localStorage is unavailable
  }
  return new Set(COLUMN_PRESETS.Overview);
}

export function saveColumns(colSet) {
  try {
    localStorage.setItem(STORAGE_KEY_COLS, JSON.stringify([...colSet]));
  } catch {
    // Ignore
  }
}

export function detectActivePreset(colSet) {
  for (const [name, list] of Object.entries(COLUMN_PRESETS)) {
    if (list.length === colSet.size && list.every((k) => colSet.has(k))) {
      return name;
    }
  }
  return null;
}
