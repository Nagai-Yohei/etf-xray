/**
 * Pure filtering, sorting, summary, URL-state, and CSV helpers for the Cross-ETF Stock Screener.
 * Zero DOM dependencies so it can be unit-tested cleanly in headless JS runtimes.
 */

export const SECTOR_GROUP_LABELS = {
  standard: "Standard Corporate",
  bank: "Bank",
  insurance: "Insurance",
  asset_mgmt: "Financial / Asset Mgmt",
  reit: "REIT",
  utility: "Utility",
  energy_upstream: "Energy (Upstream / Integrated)",
  telecom: "Telecom",
};

export const SECTOR_OPTIONS = [
  { value: "", label: "All Sectors" },
  { value: "standard", label: "Standard Corporate" },
  { value: "non_financial", label: "Exclude Financials & REITs" },
  { value: "bank", label: "Bank" },
  { value: "insurance", label: "Insurance" },
  { value: "asset_mgmt", label: "Financial / Asset Mgmt" },
  { value: "reit", label: "REIT" },
  { value: "utility", label: "Utility" },
  { value: "energy_upstream", label: "Energy (Upstream / Integrated)" },
  { value: "telecom", label: "Telecom" },
];

const FINANCIAL_AND_REIT_GROUPS = new Set(["bank", "insurance", "asset_mgmt", "reit"]);

export const DEFAULT_FILTERS = Object.freeze({
  scope: "active", // "active" | "all" | "former"
  etfs: [], // e.g. ["SCHD", "HDV"]
  etfMatchMode: "any", // "any" | "all"
  minEtfs: "",
  sector: "",
  search: "",
  minYield: "", // percentage points, e.g. "3.0" -> 0.03
  maxYield: "",
  minStreak: "", // years
  minDivCagr5y: "", // percentage points
  maxPayoutEps: "", // percentage points
  maxPayoutFcf: "", // percentage points
  maxPe: "", // multiple
  minRoic: "", // percentage points
  minOpMargin: "", // percentage points
  minRevCagr5y: "", // percentage points
  maxShareChange5y: "", // percentage points (negative = buybacks)
});

export const SCREENER_PRESETS = [
  {
    id: "all_active",
    label: "All Active",
    description: "All securities currently held in at least one covered ETF",
    filters: { scope: "active" },
    sortKey: "etf_count",
    sortDir: -1,
    colSet: "Overview",
  },
  {
    id: "aristocrat_style",
    label: "Dividend Aristocrat Style",
    description: "10+ yr streak, Yield ≥ 2%, EPS Payout ≤ 80%",
    filters: { scope: "active", minStreak: "10", minYield: "2", maxPayoutEps: "80" },
    sortKey: "div_streak_years",
    sortDir: -1,
    colSet: "Dividend",
  },
  {
    id: "high_yield_covered",
    label: "High Yield & Covered",
    description: "Yield ≥ 3.5% with FCF Payout ≤ 85%",
    filters: { scope: "active", minYield: "3.5", maxPayoutFcf: "85" },
    sortKey: "ttm_yield",
    sortDir: -1,
    colSet: "Dividend",
  },
  {
    id: "div_growth",
    label: "Dividend Growth",
    description: "5+ yr streak, 5Y Div CAGR ≥ 7%, Yield ≥ 1.5%",
    filters: { scope: "active", minStreak: "5", minDivCagr5y: "7", minYield: "1.5" },
    sortKey: "div_cagr_5y",
    sortDir: -1,
    colSet: "Dividend",
  },
  {
    id: "quality_compounders",
    label: "Quality Compounders",
    description: "ROIC ≥ 15%, Op Margin ≥ 15%, 5Y Rev CAGR ≥ 5%",
    filters: { scope: "active", minRoic: "15", minOpMargin: "15", minRevCagr5y: "5" },
    sortKey: "roic",
    sortDir: -1,
    colSet: "Fundamentals",
  },
  {
    id: "buyback_champions",
    label: "Buyback Champions",
    description: "5Y Share Count ≤ −10% with ROIC ≥ 12%",
    filters: { scope: "active", maxShareChange5y: "-10", minRoic: "12" },
    sortKey: "share_change_5y",
    sortDir: 1,
    colSet: "Fundamentals",
  },
  {
    id: "multi_etf_core",
    label: "Multi-ETF Core (3+)",
    description: "Currently held simultaneously across 3 or more covered ETFs",
    filters: { scope: "active", minEtfs: "3" },
    sortKey: "etf_count",
    sortDir: -1,
    colSet: "Overview",
  },
];

export function createFilters(overrides = {}) {
  return {
    ...DEFAULT_FILTERS,
    ...overrides,
    etfs: Array.isArray(overrides.etfs) ? [...overrides.etfs] : [],
  };
}

export function parseOptionalNumber(raw) {
  if (raw === null || raw === undefined) return null;
  const trimmed = String(raw).trim();
  if (!trimmed) return null;
  const num = Number(trimmed);
  return Number.isFinite(num) ? num : null;
}

export function sectorLabel(sectorGroup) {
  if (!sectorGroup) return SECTOR_GROUP_LABELS.standard;
  return SECTOR_GROUP_LABELS[sectorGroup] || sectorGroup;
}

export function filterStocks(stocksList, filters) {
  const f = filters || DEFAULT_FILTERS;
  const q = String(f.search || "").trim().toLowerCase();
  const selectedEtfs = Array.isArray(f.etfs) ? f.etfs : [];
  const minEtfs = parseOptionalNumber(f.minEtfs);
  const minYield = parseOptionalNumber(f.minYield);
  const maxYield = parseOptionalNumber(f.maxYield);
  const minStreak = parseOptionalNumber(f.minStreak);
  const minDivCagr5y = parseOptionalNumber(f.minDivCagr5y);
  const maxPayoutEps = parseOptionalNumber(f.maxPayoutEps);
  const maxPayoutFcf = parseOptionalNumber(f.maxPayoutFcf);
  const maxPe = parseOptionalNumber(f.maxPe);
  const minRoic = parseOptionalNumber(f.minRoic);
  const minOpMargin = parseOptionalNumber(f.minOpMargin);
  const minRevCagr5y = parseOptionalNumber(f.minRevCagr5y);
  const maxShareChange5y = parseOptionalNumber(f.maxShareChange5y);

  const out = [];
  for (const s of stocksList || []) {
    const activeEtfs = Array.isArray(s.current_etfs) ? s.current_etfs : [];
    const allEtfs = Array.isArray(s.all_etfs) ? s.all_etfs : activeEtfs;

    // 1. Universe scope
    if (f.scope === "active" && activeEtfs.length === 0) continue;
    if (f.scope === "former" && activeEtfs.length > 0) continue;

    // 2. ETF membership filter (against current ETFs for active scope, all ETFs otherwise)
    const refEtfs = f.scope === "active" ? activeEtfs : allEtfs;
    if (selectedEtfs.length > 0) {
      if (f.etfMatchMode === "all") {
        if (!selectedEtfs.every((id) => refEtfs.includes(id))) continue;
      } else {
        if (!selectedEtfs.some((id) => refEtfs.includes(id))) continue;
      }
    }

    // 3. Min ETF count
    if (minEtfs !== null && refEtfs.length < minEtfs) continue;

    // 4. Sector group filter
    if (f.sector) {
      const sg = s.sector_group || null;
      if (f.sector === "standard") {
        if (sg !== null) continue;
      } else if (f.sector === "non_financial") {
        if (sg !== null && FINANCIAL_AND_REIT_GROUPS.has(sg)) continue;
      } else if (sg !== f.sector) {
        continue;
      }
    }

    // 5. Text search
    if (q) {
      const tk = (s.ticker || s.security_id || "").toLowerCase();
      const nm = (s.name || "").toLowerCase();
      if (!tk.includes(q) && !nm.includes(q)) continue;
    }

    // 6. Numeric fundamental / dividend thresholds
    if (minYield !== null && (s.ttm_yield === null || s.ttm_yield === undefined || s.ttm_yield * 100 < minYield)) {
      continue;
    }
    if (maxYield !== null && (s.ttm_yield === null || s.ttm_yield === undefined || s.ttm_yield * 100 > maxYield)) {
      continue;
    }
    if (minStreak !== null && (s.div_streak_years === null || s.div_streak_years === undefined || s.div_streak_years < minStreak)) {
      continue;
    }
    if (minDivCagr5y !== null && (s.div_cagr_5y === null || s.div_cagr_5y === undefined || s.div_cagr_5y * 100 < minDivCagr5y)) {
      continue;
    }
    if (maxPayoutEps !== null && (s.payout_ratio_eps === null || s.payout_ratio_eps === undefined || s.payout_ratio_eps * 100 > maxPayoutEps)) {
      continue;
    }
    if (maxPayoutFcf !== null && (s.payout_ratio_fcf === null || s.payout_ratio_fcf === undefined || s.payout_ratio_fcf * 100 > maxPayoutFcf)) {
      continue;
    }
    if (maxPe !== null && (s.pe_ratio === null || s.pe_ratio === undefined || s.pe_ratio <= 0 || s.pe_ratio > maxPe)) {
      continue;
    }
    if (minRoic !== null && (s.roic === null || s.roic === undefined || s.roic * 100 < minRoic)) {
      continue;
    }
    if (minOpMargin !== null && (s.op_margin === null || s.op_margin === undefined || s.op_margin * 100 < minOpMargin)) {
      continue;
    }
    if (minRevCagr5y !== null && (s.rev_cagr_5y === null || s.rev_cagr_5y === undefined || s.rev_cagr_5y * 100 < minRevCagr5y)) {
      continue;
    }
    if (maxShareChange5y !== null && (s.share_change_5y === null || s.share_change_5y === undefined || s.share_change_5y * 100 > maxShareChange5y)) {
      continue;
    }

    out.push(s);
  }
  return out;
}

export function getSortValue(row, key) {
  if (key === "etf_count") return (row.current_etfs || []).length;
  if (key === "ticker") return row.ticker || row.security_id || "";
  if (key === "name") return row.name || "";
  if (key === "sector_group") return sectorLabel(row.sector_group);
  return row[key];
}

export function compareNullable(a, b, dir = -1) {
  const aNull = a === null || a === undefined || (typeof a === "number" && !Number.isFinite(a));
  const bNull = b === null || b === undefined || (typeof b === "number" && !Number.isFinite(b));
  if (aNull && bNull) return 0;
  if (aNull) return 1;
  if (bNull) return -1;
  if (typeof a === "string" && typeof b === "string") {
    return a.localeCompare(b) * dir;
  }
  return (Number(a) - Number(b)) * dir;
}

export function sortStocks(rows, sortKey, sortDir = -1) {
  return [...rows].sort((a, b) => {
    const primary = compareNullable(getSortValue(a, sortKey), getSortValue(b, sortKey), sortDir);
    if (primary !== 0) return primary;
    const byEtfs = ((b.current_etfs || []).length) - ((a.current_etfs || []).length);
    if (byEtfs !== 0) return byEtfs;
    const byRev = compareNullable(a.revenue_ttm, b.revenue_ttm, -1);
    if (byRev !== 0) return byRev;
    return (a.ticker || a.security_id || "").localeCompare(b.ticker || b.security_id || "");
  });
}

function medianOf(values) {
  const valid = values
    .filter((v) => v !== null && v !== undefined && Number.isFinite(Number(v)))
    .map(Number)
    .sort((a, b) => a - b);
  if (valid.length === 0) return null;
  const mid = Math.floor(valid.length / 2);
  return valid.length % 2 === 1 ? valid[mid] : (valid[mid - 1] + valid[mid]) / 2;
}

export function computeScreenerSummary(rows) {
  const dividendPayers = rows.filter((r) => r.ttm_yield !== null && r.ttm_yield !== undefined && r.ttm_yield > 0);
  return {
    count: rows.length,
    dividendPayerCount: dividendPayers.length,
    medianYield: medianOf(dividendPayers.map((r) => r.ttm_yield)),
    medianStreak: medianOf(rows.filter((r) => (r.div_streak_years || 0) > 0).map((r) => r.div_streak_years)),
    medianPe: medianOf(rows.filter((r) => r.pe_ratio !== null && r.pe_ratio > 0).map((r) => r.pe_ratio)),
    medianRoic: medianOf(rows.map((r) => r.roic)),
  };
}

const FILTER_COMPARE_KEYS = Object.keys(DEFAULT_FILTERS).filter((k) => k !== "search");

export function detectActiveScreenerPreset(filters) {
  for (const preset of SCREENER_PRESETS) {
    const target = createFilters(preset.filters);
    let matches = true;
    for (const k of FILTER_COMPARE_KEYS) {
      if (k === "etfs") {
        const a = [...(filters.etfs || [])].sort().join(",");
        const b = [...(target.etfs || [])].sort().join(",");
        if (a !== b) {
          matches = false;
          break;
        }
      } else if (String(filters[k] ?? "") !== String(target[k] ?? "")) {
        matches = false;
        break;
      }
    }
    if (matches && !String(filters.search || "").trim()) {
      return preset.id;
    }
  }
  return null;
}

export function describeActiveFilters(filters) {
  const chips = [];
  if (filters.scope === "all") chips.push({ key: "scope", label: "Scope: All Tracked" });
  if (filters.scope === "former") chips.push({ key: "scope", label: "Scope: Former Only" });
  if (filters.etfs && filters.etfs.length > 0) {
    const mode = filters.etfs.length > 1 ? ` (${filters.etfMatchMode.toUpperCase()})` : "";
    chips.push({ key: "etfs", label: `ETFs: ${filters.etfs.join(", ")}${mode}` });
  }
  if (filters.minEtfs) chips.push({ key: "minEtfs", label: `ETFs ≥ ${filters.minEtfs}` });
  if (filters.sector) {
    const opt = SECTOR_OPTIONS.find((o) => o.value === filters.sector);
    chips.push({ key: "sector", label: `Sector: ${opt ? opt.label : filters.sector}` });
  }
  if (filters.search && filters.search.trim()) {
    chips.push({ key: "search", label: `Query: "${filters.search.trim()}"` });
  }
  if (filters.minYield) chips.push({ key: "minYield", label: `Yield ≥ ${filters.minYield}%` });
  if (filters.maxYield) chips.push({ key: "maxYield", label: `Yield ≤ ${filters.maxYield}%` });
  if (filters.minStreak) chips.push({ key: "minStreak", label: `Streak ≥ ${filters.minStreak}Y` });
  if (filters.minDivCagr5y) chips.push({ key: "minDivCagr5y", label: `5Y Div CAGR ≥ ${filters.minDivCagr5y}%` });
  if (filters.maxPayoutEps) chips.push({ key: "maxPayoutEps", label: `EPS Payout ≤ ${filters.maxPayoutEps}%` });
  if (filters.maxPayoutFcf) chips.push({ key: "maxPayoutFcf", label: `FCF Payout ≤ ${filters.maxPayoutFcf}%` });
  if (filters.maxPe) chips.push({ key: "maxPe", label: `P/E ≤ ${filters.maxPe}x` });
  if (filters.minRoic) chips.push({ key: "minRoic", label: `ROIC ≥ ${filters.minRoic}%` });
  if (filters.minOpMargin) chips.push({ key: "minOpMargin", label: `Op Margin ≥ ${filters.minOpMargin}%` });
  if (filters.minRevCagr5y) chips.push({ key: "minRevCagr5y", label: `5Y Rev CAGR ≥ ${filters.minRevCagr5y}%` });
  if (filters.maxShareChange5y) chips.push({ key: "maxShareChange5y", label: `5Y Share Δ ≤ ${filters.maxShareChange5y}%` });
  return chips;
}

const QUERY_PARAM_KEYS = [
  "scope",
  "etfMatchMode",
  "minEtfs",
  "sector",
  "search",
  "minYield",
  "maxYield",
  "minStreak",
  "minDivCagr5y",
  "maxPayoutEps",
  "maxPayoutFcf",
  "maxPe",
  "minRoic",
  "minOpMargin",
  "minRevCagr5y",
  "maxShareChange5y",
];

export function serializeScreenerQuery(state) {
  const params = new URLSearchParams();
  const f = state.filters || DEFAULT_FILTERS;
  for (const k of QUERY_PARAM_KEYS) {
    const val = String(f[k] ?? "").trim();
    if (val && val !== String(DEFAULT_FILTERS[k])) {
      params.set(k, val);
    }
  }
  if (Array.isArray(f.etfs) && f.etfs.length > 0) {
    params.set("etfs", f.etfs.join(","));
  }
  if (state.sortKey && state.sortKey !== "etf_count") {
    params.set("sort", state.sortKey);
  }
  if (state.sortDir === 1) {
    params.set("dir", "asc");
  }
  if (state.colSet && state.colSet !== "Overview") {
    params.set("cols", state.colSet);
  }
  const qs = params.toString();
  return qs ? `#/screener?${qs}` : "#/screener";
}

export function parseScreenerQuery(params, validEtfIds = []) {
  const validSet = new Set(validEtfIds);
  const filters = createFilters();
  if (params) {
    for (const k of QUERY_PARAM_KEYS) {
      const val = params.get(k);
      if (val !== null && val !== "") {
        if (k === "scope" && !["active", "all", "former"].includes(val)) continue;
        if (k === "etfMatchMode" && !["any", "all"].includes(val)) continue;
        filters[k] = val;
      }
    }
    const rawEtfs = params.get("etfs");
    if (rawEtfs) {
      filters.etfs = rawEtfs
        .split(",")
        .map((s) => s.trim().toUpperCase())
        .filter((id) => id && (validSet.size === 0 || validSet.has(id)));
    }
  }
  const sortKey = (params && params.get("sort")) || "etf_count";
  const sortDir = params && params.get("dir") === "asc" ? 1 : -1;
  const colSet = (params && params.get("cols")) || "Overview";
  return { filters, sortKey, sortDir, colSet };
}

function escapeCsvCell(val) {
  if (val === null || val === undefined) return "";
  let str = String(val);
  // Mitigate spreadsheet formula injection when opened in Excel / Google Sheets
  if (/^[=+\-@\t\r]/.test(str)) {
    str = `'${str}`;
  }
  if (/[",\r\n]/.test(str)) {
    return `"${str.replace(/"/g, '""')}"`;
  }
  return str;
}

export function buildScreenerCsv(rows, columns) {
  const headers = ["Ticker", "Company", "Current ETFs", "Sector", ...columns.map((c) => c.label)];
  const lines = [headers.map(escapeCsvCell).join(",")];
  for (const r of rows) {
    const base = [
      r.ticker || r.security_id || "",
      r.name || "",
      (r.current_etfs || []).join(" "),
      sectorLabel(r.sector_group),
    ];
    const metrics = columns.map((c) => {
      const raw = c.get(r);
      if (raw === null || raw === undefined || !Number.isFinite(Number(raw))) return "";
      if (c.csvPct) return (Number(raw) * 100).toFixed(2);
      if (c.csvDecimals !== undefined) return Number(raw).toFixed(c.csvDecimals);
      return String(raw);
    });
    lines.push([...base, ...metrics].map(escapeCsvCell).join(","));
  }
  return lines.join("\r\n");
}
