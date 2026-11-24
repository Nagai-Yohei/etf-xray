/**
 * Pure calculation, formatting, and table helpers shared by Stock View.
 */

import { el } from "./dom.js";

export const SECTOR_LABELS = {
  reit: "REIT",
  bank: "Bank",
  insurance: "Insurance",
  asset_mgmt: "Asset Mgmt / Financial",
  utility: "Utility",
  energy_upstream: "Energy",
  telecom: "Telecom",
};

export const FINANCIAL_SECTORS = new Set(["bank", "insurance", "asset_mgmt"]);

export function effectiveDps(row) {
  if (row.dps_declared !== null && row.dps_declared !== undefined) return row.dps_declared;
  if (row.dps_paid !== null && row.dps_paid !== undefined) return row.dps_paid;
  const shDil = row.shares_diluted;
  const shOut = row.shares_outstanding;
  const sh =
    shDil && shOut && shOut > shDil * 1.3 && shOut < shDil * 20
      ? shOut
      : shDil || shOut;
  if (row.dividends_paid !== null && row.dividends_paid !== undefined && sh > 0) {
    return row.dividends_paid / sh;
  }
  return null;
}

export function hasAnyValue(arr) {
  return Array.isArray(arr) && arr.some((v) => v !== null && v !== undefined && Number.isFinite(Number(v)));
}

export function fmtSharesM(val) {
  if (val === null || val === undefined || !Number.isFinite(Number(val))) return "—";
  return `${(Number(val) / 1e6).toFixed(1)}M`;
}

export function calcAnnualRoic(r) {
  const ebit =
    r.operating_income != null
      ? r.operating_income
      : r.pretax_income != null
        ? r.pretax_income + (r.interest_expense || 0)
        : null;
  if (ebit == null || r.total_equity == null) return null;
  let taxRate = 0.21;
  if (r.pretax_income > 0 && r.income_tax != null) {
    taxRate = Math.max(0, Math.min(0.4, r.income_tax / r.pretax_income));
  }
  const nopat = ebit * (1 - taxRate);
  const invCap = r.total_equity + (r.total_debt || 0) - (r.cash || 0);
  return invCap > 0 ? nopat / invCap : null;
}

export function calcInterestCoverage(r) {
  const ebit =
    r.operating_income != null
      ? r.operating_income
      : r.pretax_income != null
        ? r.pretax_income + (r.interest_expense || 0)
        : null;
  if (ebit == null || !(r.interest_expense > 0)) return null;
  return ebit / r.interest_expense;
}

export function buildTable(headers, rows, compact = false) {
  const thead = el("thead", {}, [
    el(
      "tr",
      {},
      headers.map((h) => el("th", { className: h.num ? "num" : "", text: h.label }))
    ),
  ]);
  const tbody = el(
    "tbody",
    {},
    rows.map((cells) =>
      el(
        "tr",
        {},
        cells.map((cell, idx) =>
          el("td", { className: headers[idx].num ? "num" : "", text: String(cell) })
        )
      )
    )
  );
  return el(
    "div",
    { className: `table-scroll${compact ? " stock-table-compact" : ""}` },
    [el("table", { className: "data-table" }, [thead, tbody])]
  );
}
