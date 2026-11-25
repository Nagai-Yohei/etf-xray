/**
 * Cross-ETF Stock Screener (#/screener).
 * Screens all covered ETF constituents (active and historical) across SEC XBRL fundamentals,
 * dividend history, valuation multiples, accounting sectors, and multi-ETF membership.
 */

import { destroyAllCharts } from "./charts.js";
import {
  clearNode,
  el,
  fmtInt,
  fmtMultiple,
  fmtPct,
  fmtPerShare,
  fmtPrice,
  fmtStreak,
  fmtUsdCompact,
} from "./dom.js";
import {
  buildScreenerCsv,
  computeScreenerSummary,
  createFilters,
  DEFAULT_FILTERS,
  describeActiveFilters,
  detectActiveScreenerPreset,
  filterStocks,
  parseScreenerQuery,
  SCREENER_PRESETS,
  SECTOR_OPTIONS,
  sectorLabel,
  serializeScreenerQuery,
  sortStocks,
} from "./screener-helpers.js";

const COLUMN_SETS = {
  Overview: [
    { key: "latest_price", label: "Price", csvDecimals: 2, get: (r) => r.latest_price, fmt: (v) => fmtPrice(v) },
    { key: "ttm_yield", label: "TTM Yield", csvPct: true, get: (r) => r.ttm_yield, fmt: (v) => fmtPct(v, 2) },
    { key: "div_streak_years", label: "Div Streak", csvDecimals: 0, get: (r) => r.div_streak_years, fmt: (v, r) => fmtStreak(v, r.div_streak_open) },
    { key: "div_cagr_5y", label: "5Y Div CAGR", csvPct: true, get: (r) => r.div_cagr_5y, fmt: (v) => fmtPct(v, 1, true) },
    { key: "payout_ratio_eps", label: "Payout (EPS)", csvPct: true, get: (r) => r.payout_ratio_eps, fmt: (v) => fmtPct(v, 1) },
    { key: "pe_ratio", label: "P/E (TTM)", csvDecimals: 2, get: (r) => r.pe_ratio, fmt: (v) => fmtMultiple(v) },
    { key: "roic", label: "ROIC", csvPct: true, get: (r) => r.roic, fmt: (v) => fmtPct(v, 1) },
    { key: "revenue_ttm", label: "TTM Rev", csvDecimals: 0, get: (r) => r.revenue_ttm, fmt: (v) => fmtUsdCompact(v) },
  ],
  Dividend: [
    { key: "ttm_yield", label: "TTM Yield", csvPct: true, get: (r) => r.ttm_yield, fmt: (v) => fmtPct(v, 2) },
    { key: "div_streak_years", label: "Div Streak", csvDecimals: 0, get: (r) => r.div_streak_years, fmt: (v, r) => fmtStreak(v, r.div_streak_open) },
    { key: "div_cagr_5y", label: "5Y Div CAGR", csvPct: true, get: (r) => r.div_cagr_5y, fmt: (v) => fmtPct(v, 1, true) },
    { key: "div_cagr_10y", label: "10Y Div CAGR", csvPct: true, get: (r) => r.div_cagr_10y, fmt: (v) => fmtPct(v, 1, true) },
    { key: "payout_ratio_eps", label: "Payout (EPS)", csvPct: true, get: (r) => r.payout_ratio_eps, fmt: (v) => fmtPct(v, 1) },
    { key: "payout_ratio_fcf", label: "Payout (FCF)", csvPct: true, get: (r) => r.payout_ratio_fcf, fmt: (v) => fmtPct(v, 1) },
    { key: "dps_ttm", label: "TTM DPS", csvDecimals: 2, get: (r) => r.dps_ttm, fmt: (v) => fmtPerShare(v) },
  ],
  Fundamentals: [
    { key: "revenue_ttm", label: "TTM Rev", csvDecimals: 0, get: (r) => r.revenue_ttm, fmt: (v) => fmtUsdCompact(v) },
    { key: "rev_cagr_5y", label: "5Y Rev CAGR", csvPct: true, get: (r) => r.rev_cagr_5y, fmt: (v) => fmtPct(v, 1, true) },
    { key: "gross_margin", label: "Gross Margin", csvPct: true, get: (r) => r.gross_margin, fmt: (v) => fmtPct(v, 1) },
    { key: "op_margin", label: "Op Margin", csvPct: true, get: (r) => r.op_margin, fmt: (v) => fmtPct(v, 1) },
    { key: "fcf_ex_sbc_margin", label: "Adj. FCF Mgn", csvPct: true, get: (r) => r.fcf_ex_sbc_margin, fmt: (v) => fmtPct(v, 1) },
    { key: "roic", label: "ROIC", csvPct: true, get: (r) => r.roic, fmt: (v) => fmtPct(v, 1) },
    { key: "roe", label: "ROE", csvPct: true, get: (r) => r.roe, fmt: (v) => fmtPct(v, 1) },
    { key: "share_change_5y", label: "5Y Share Δ", csvPct: true, get: (r) => r.share_change_5y, fmt: (v) => fmtPct(v, 1, true) },
  ],
  Valuation: [
    { key: "latest_price", label: "Price", csvDecimals: 2, get: (r) => r.latest_price, fmt: (v) => fmtPrice(v) },
    { key: "pe_ratio", label: "P/E (TTM)", csvDecimals: 2, get: (r) => r.pe_ratio, fmt: (v) => fmtMultiple(v) },
    { key: "pb_ratio", label: "P/B", csvDecimals: 2, get: (r) => r.pb_ratio, fmt: (v) => fmtMultiple(v) },
    { key: "ttm_yield", label: "TTM Yield", csvPct: true, get: (r) => r.ttm_yield, fmt: (v) => fmtPct(v, 2) },
    { key: "dps_ttm", label: "TTM DPS", csvDecimals: 2, get: (r) => r.dps_ttm, fmt: (v) => fmtPerShare(v) },
    { key: "net_debt_to_fcf", label: "Net Debt/FCF", csvDecimals: 2, get: (r) => r.net_debt_to_fcf, fmt: (v) => fmtMultiple(v) },
    { key: "equity_ratio", label: "Equity Ratio", csvPct: true, get: (r) => r.equity_ratio, fmt: (v) => fmtPct(v, 1) },
  ],
};

const NUMERIC_FILTER_FIELDS = [
  { key: "minYield", label: "Min Yield", unit: "%", placeholder: "e.g. 2.5", step: "0.5" },
  { key: "maxYield", label: "Max Yield", unit: "%", placeholder: "e.g. 8", step: "0.5" },
  { key: "minStreak", label: "Min Div Streak", unit: "yrs", placeholder: "e.g. 10", step: "1" },
  { key: "minDivCagr5y", label: "Min 5Y Div CAGR", unit: "%", placeholder: "e.g. 5", step: "1" },
  { key: "maxPayoutEps", label: "Max Payout (EPS)", unit: "%", placeholder: "e.g. 80", step: "5" },
  { key: "maxPayoutFcf", label: "Max Payout (FCF)", unit: "%", placeholder: "e.g. 85", step: "5" },
  { key: "maxPe", label: "Max P/E (TTM)", unit: "x", placeholder: "e.g. 25", step: "1" },
  { key: "minRoic", label: "Min ROIC", unit: "%", placeholder: "e.g. 12", step: "1" },
  { key: "minOpMargin", label: "Min Op Margin", unit: "%", placeholder: "e.g. 15", step: "1" },
  { key: "minRevCagr5y", label: "Min 5Y Rev CAGR", unit: "%", placeholder: "e.g. 5", step: "1" },
  { key: "maxShareChange5y", label: "Max 5Y Share Δ", unit: "%", placeholder: "e.g. 0", step: "1" },
];

export async function renderScreenerView(
  root,
  metaData,
  stocksList,
  queryParams,
  navigate,
  onStateChange
) {
  destroyAllCharts();
  clearNode(root);

  const etfIds = (metaData?.etfs || []).map((e) => e.etf_id);
  const activeTotal = stocksList.filter((s) => (s.current_etfs || []).length > 0).length;
  const formerTotal = stocksList.length - activeTotal;

  const initial = parseScreenerQuery(queryParams, etfIds);
  const validColSet = COLUMN_SETS[initial.colSet] ? initial.colSet : "Overview";

  const state = {
    filters: initial.filters,
    sortKey: initial.sortKey,
    sortDir: initial.sortDir,
    colSet: validColSet,
    page: 1,
    pageSize: 25,
    tableExpanded: false,
    filtersCollapsed: false,
  };

  let lastMatchedRows = [];

  function syncUrl() {
    const targetHash = serializeScreenerQuery(state);
    if (window.location.hash !== targetHash) {
      window.history.replaceState(null, "", targetHash);
    }
    document.title = "Stock Screener — ETF X-Ray";
    if (typeof onStateChange === "function") {
      onStateChange(targetHash);
    }
  }

  // Persistent control references (so inputs never lose focus while typing)
  const numericInputs = new Map();
  let searchInput = null;
  let sectorSelect = null;
  let minEtfsSelect = null;
  let scopeButtons = new Map();
  let etfButtons = new Map();
  let etfModeWrap = null;
  let etfModeButtons = new Map();
  let presetButtons = new Map();

  // Dynamic slots
  const activeChipsSlot = el("div", { className: "screener-active-chips" });
  const kpiSlot = el("div", { className: "kpi-grid screener-kpi-grid" });
  const resultsCard = el("section", { className: "card" });

  // Back row
  root.appendChild(
    el("div", { className: "back-row" }, [
      el("a", {
        href: "#/",
        className: "back-link",
        text: "← Back to Home",
        onClick: (e) => {
          e.preventDefault();
          navigate("#/");
        },
      }),
    ])
  );

  // Header + Presets card
  const presetStrip = el(
    "div",
    { className: "screener-presets" },
    SCREENER_PRESETS.map((p) => {
      const btn = el("button", {
        type: "button",
        className: "btn btn-sm",
        text: p.label,
        title: p.description,
        onClick: () => {
          state.filters = createFilters(p.filters);
          state.sortKey = p.sortKey;
          state.sortDir = p.sortDir;
          state.colSet = p.colSet;
          state.page = 1;
          syncControlsFromState();
          refreshResults();
        },
      });
      presetButtons.set(p.id, btn);
      return btn;
    })
  );

  root.appendChild(
    el("section", { className: "card" }, [
      el("div", { className: "card-header compare-header-top" }, [
        el("div", {}, [
          el("h1", { className: "page-title", text: "Cross-ETF Stock Screener" }),
          el("p", {
            className: "page-subtitle",
            text: `Filter ${fmtInt(activeTotal)} active (${fmtInt(stocksList.length)} total tracked) securities across ${etfIds.length} ETFs using SEC XBRL fundamentals and dividend history.`,
          }),
        ]),
        el("a", {
          href: "#/compare",
          className: "btn btn-sm",
          text: "Compare ETF Overlap →",
          onClick: (e) => {
            e.preventDefault();
            navigate("#/compare");
          },
        }),
      ]),
      el("div", { className: "card-body" }, [
        el("div", { className: "screener-preset-row" }, [
          el("span", { className: "compare-quick-label text-muted", text: "Strategy presets:" }),
          presetStrip,
        ]),
      ]),
    ])
  );

  // Filter Panel card
  const filterBody = el("div", { className: "card-body screener-filter-body" });
  const toggleFiltersBtn = el("button", {
    type: "button",
    className: "btn btn-sm",
    text: "Hide Filters",
    onClick: () => {
      state.filtersCollapsed = !state.filtersCollapsed;
      filterBody.classList.toggle("hidden", state.filtersCollapsed);
      toggleFiltersBtn.textContent = state.filtersCollapsed ? "Show Filters" : "Hide Filters";
    },
  });
  const resetBtn = el("button", {
    type: "button",
    className: "btn btn-sm",
    text: "Reset All",
    onClick: () => {
      state.filters = createFilters();
      state.sortKey = "etf_count";
      state.sortDir = -1;
      state.page = 1;
      syncControlsFromState();
      refreshResults();
    },
  });

  // Row 1: Scope, ETF membership, Min ETFs, Sector
  const scopeOptions = [
    { id: "active", label: `Active (${fmtInt(activeTotal)})` },
    { id: "all", label: `All (${fmtInt(stocksList.length)})` },
    { id: "former", label: `Former (${fmtInt(formerTotal)})` },
  ];
  const scopeGroup = el(
    "div",
    { className: "tab-bar" },
    scopeOptions.map((opt) => {
      const btn = el("button", {
        type: "button",
        className: "btn btn-sm",
        text: opt.label,
        onClick: () => {
          state.filters.scope = opt.id;
          state.page = 1;
          syncControlsFromState();
          refreshResults();
        },
      });
      scopeButtons.set(opt.id, btn);
      return btn;
    })
  );

  const etfPills = el(
    "div",
    { className: "screener-etf-pills" },
    etfIds.map((id) => {
      const btn = el("button", {
        type: "button",
        className: "btn btn-sm",
        text: id,
        onClick: () => {
          const idx = state.filters.etfs.indexOf(id);
          if (idx >= 0) {
            state.filters.etfs.splice(idx, 1);
          } else {
            state.filters.etfs.push(id);
          }
          state.page = 1;
          syncControlsFromState();
          refreshResults();
        },
      });
      etfButtons.set(id, btn);
      return btn;
    })
  );

  etfModeWrap = el("div", { className: "screener-etf-mode" }, [
    el("span", { className: "text-muted screener-mini-label", text: "Match:" }),
    ...["any", "all"].map((m) => {
      const btn = el("button", {
        type: "button",
        className: "btn btn-sm",
        text: m === "any" ? "Any" : "All",
        onClick: () => {
          state.filters.etfMatchMode = m;
          state.page = 1;
          syncControlsFromState();
          refreshResults();
        },
      });
      etfModeButtons.set(m, btn);
      return btn;
    }),
  ]);

  minEtfsSelect = el(
    "select",
    {
      className: "select-control screener-select",
      ariaLabel: "Minimum ETFs holding stock",
      onChange: (e) => {
        state.filters.minEtfs = e.target.value;
        state.page = 1;
        refreshResults();
      },
    },
    [
      el("option", { value: "", text: "Any count" }),
      ...Array.from({ length: Math.max(0, etfIds.length - 1) }, (_, idx) => idx + 2).map((n) =>
        el("option", { value: String(n), text: n === etfIds.length ? `All ${n} ETFs` : `${n}+ ETFs` })
      ),
    ]
  );

  sectorSelect = el(
    "select",
    {
      className: "select-control screener-select",
      ariaLabel: "Accounting sector group",
      onChange: (e) => {
        state.filters.sector = e.target.value;
        state.page = 1;
        refreshResults();
      },
    },
    SECTOR_OPTIONS.map((opt) => el("option", { value: opt.value, text: opt.label }))
  );

  searchInput = el("input", {
    type: "search",
    className: "search-input screener-search-input",
    placeholder: "Filter ticker or company name...",
    ariaLabel: "Filter screener by ticker or company name",
    onInput: (e) => {
      state.filters.search = e.target.value;
      state.page = 1;
      refreshResults();
    },
  });

  const universeRow = el("div", { className: "screener-universe-row" }, [
    el("div", { className: "screener-field-block" }, [
      el("span", { className: "screener-field-label", text: "Universe" }),
      scopeGroup,
    ]),
    el("div", { className: "screener-field-block" }, [
      el("span", { className: "screener-field-label", text: "ETF Membership" }),
      el("div", { className: "screener-etf-row" }, [etfPills, etfModeWrap]),
    ]),
    el("div", { className: "screener-field-block" }, [
      el("label", { className: "screener-field-label", text: "Min ETFs" }),
      minEtfsSelect,
    ]),
    el("div", { className: "screener-field-block" }, [
      el("label", { className: "screener-field-label", text: "Sector Group" }),
      sectorSelect,
    ]),
    el("div", { className: "screener-field-block screener-search-block" }, [
      el("label", { className: "screener-field-label", text: "Search" }),
      searchInput,
    ]),
  ]);

  // Row 2: Numeric fundamental & dividend thresholds
  const numericGrid = el(
    "div",
    { className: "screener-numeric-grid" },
    NUMERIC_FILTER_FIELDS.map((field) => {
      const inp = el("input", {
        type: "number",
        step: field.step,
        className: "screener-num-input",
        placeholder: field.placeholder,
        ariaLabel: field.label,
        onInput: (e) => {
          state.filters[field.key] = e.target.value;
          state.page = 1;
          refreshResults();
        },
      });
      numericInputs.set(field.key, inp);
      return el("div", { className: "screener-num-field" }, [
        el("label", { className: "screener-field-label", text: field.label }),
        el("div", { className: "screener-input-unit-wrap" }, [
          inp,
          el("span", { className: "screener-input-unit", text: field.unit }),
        ]),
      ]);
    })
  );

  filterBody.appendChild(universeRow);
  filterBody.appendChild(numericGrid);
  filterBody.appendChild(activeChipsSlot);

  root.appendChild(
    el("section", { className: "card" }, [
      el("div", { className: "card-header compare-table-header" }, [
        el("h2", { className: "card-title", text: "Screening Criteria" }),
        el("div", { className: "toolbar-group" }, [resetBtn, toggleFiltersBtn]),
      ]),
      filterBody,
    ])
  );

  root.appendChild(kpiSlot);
  root.appendChild(resultsCard);

  function syncControlsFromState() {
    const f = state.filters;
    for (const [id, btn] of scopeButtons.entries()) {
      btn.classList.toggle("active", f.scope === id);
    }
    for (const [id, btn] of etfButtons.entries()) {
      btn.classList.toggle("active", f.etfs.includes(id));
    }
    etfModeWrap.classList.toggle("hidden", f.etfs.length < 2);
    for (const [m, btn] of etfModeButtons.entries()) {
      btn.classList.toggle("active", f.etfMatchMode === m);
    }
    minEtfsSelect.value = f.minEtfs || "";
    sectorSelect.value = f.sector || "";
    if (searchInput.value !== (f.search || "")) {
      searchInput.value = f.search || "";
    }
    for (const [key, inp] of numericInputs.entries()) {
      const targetVal = f[key] ?? "";
      if (inp.value !== String(targetVal)) {
        inp.value = String(targetVal);
      }
    }
  }

  function updatePresetHighlight() {
    const activePresetId = detectActiveScreenerPreset(state.filters);
    for (const [id, btn] of presetButtons.entries()) {
      btn.classList.toggle("active", id === activePresetId);
    }
  }

  function renderActiveChips() {
    clearNode(activeChipsSlot);
    const chips = describeActiveFilters(state.filters);
    if (chips.length === 0) {
      activeChipsSlot.classList.add("hidden");
      return;
    }
    activeChipsSlot.classList.remove("hidden");
    activeChipsSlot.appendChild(
      el("span", { className: "screener-mini-label text-muted", text: "Active filters:" })
    );
    for (const chip of chips) {
      activeChipsSlot.appendChild(
        el("button", {
          type: "button",
          className: "screener-chip",
          title: "Click to remove filter",
          text: `${chip.label} ×`,
          onClick: () => {
            if (chip.key === "etfs") {
              state.filters.etfs = [];
            } else {
              state.filters[chip.key] = DEFAULT_FILTERS[chip.key];
            }
            state.page = 1;
            syncControlsFromState();
            refreshResults();
          },
        })
      );
    }
  }

  function renderSummaryKpis(matched) {
    clearNode(kpiSlot);
    const summary = computeScreenerSummary(matched);
    const denom =
      state.filters.scope === "active"
        ? activeTotal
        : state.filters.scope === "former"
          ? formerTotal
          : stocksList.length;
    const share = denom > 0 ? summary.count / denom : 0;

    const cards = [
      {
        label: "Matched Securities",
        value: fmtInt(summary.count),
        sub: `${fmtPct(share, 1)} of ${fmtInt(denom)} in current scope`,
        accent: true,
      },
      {
        label: "Median TTM Yield",
        value: fmtPct(summary.medianYield, 2),
        sub: `${fmtInt(summary.dividendPayerCount)} dividend payers in results`,
      },
      {
        label: "Median Div Streak",
        value: summary.medianStreak !== null ? `${summary.medianStreak.toFixed(0)} yrs` : "—",
        sub: "Among dividend-paying matches",
      },
      {
        label: "Median P/E · ROIC",
        value: `${fmtMultiple(summary.medianPe)} · ${fmtPct(summary.medianRoic, 1)}`,
        sub: "Split-adjusted SEC XBRL TTM / FY",
      },
    ];

    for (const c of cards) {
      kpiSlot.appendChild(
        el("div", { className: `kpi-card${c.accent ? " kpi-card-accent" : ""}` }, [
          el("div", { className: "kpi-label", text: c.label }),
          el("div", { className: "kpi-value", text: c.value }),
          el("div", { className: "kpi-sub text-muted", text: c.sub }),
        ])
      );
    }
  }

  function triggerCsvExport() {
    const metricCols = COLUMN_SETS[state.colSet] || COLUMN_SETS.Overview;
    const csv = buildScreenerCsv(lastMatchedRows, metricCols);
    const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `etf-xray-screener-${state.colSet.toLowerCase()}.csv`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  function renderResultsTable(sorted) {
    clearNode(resultsCard);
    const metricCols = COLUMN_SETS[state.colSet] || COLUMN_SETS.Overview;

    const totalPages = Math.max(1, Math.ceil(sorted.length / state.pageSize));
    if (state.page > totalPages) state.page = totalPages;
    const startIdx = (state.page - 1) * state.pageSize;
    const endIdx = Math.min(sorted.length, startIdx + state.pageSize);
    const pageRows = sorted.slice(startIdx, endIdx);

    // Header: Column View Tabs + Page Size + CSV + Expand toggle
    const colTabs = el(
      "div",
      { className: "tab-bar" },
      Object.keys(COLUMN_SETS).map((name) =>
        el("button", {
          type: "button",
          className: `btn btn-sm${state.colSet === name ? " active" : ""}`,
          text: name,
          onClick: () => {
            state.colSet = name;
            syncUrl();
            renderResultsTable(sorted);
          },
        })
      )
    );

    const pageSizeSel = el(
      "select",
      {
        className: "select-control screener-pagesize-select",
        ariaLabel: "Rows per page",
        onChange: (e) => {
          state.pageSize = Number(e.target.value) || 25;
          state.page = 1;
          renderResultsTable(sorted);
        },
      },
      [15, 25, 50, 100].map((n) =>
        el("option", { value: String(n), text: `${n} / page`, selected: state.pageSize === n })
      )
    );

    const exportBtn = el("button", {
      type: "button",
      className: "btn btn-sm",
      text: `Export CSV (${fmtInt(sorted.length)})`,
      disabled: sorted.length === 0,
      onClick: triggerCsvExport,
    });

    resultsCard.appendChild(
      el("div", { className: "card-header compare-table-header" }, [
        colTabs,
        el("div", { className: "toolbar-group" }, [pageSizeSel, exportBtn]),
      ])
    );

    const tableWrap = el("div", {
      className: `table-scroll screener-table-scroll${state.tableExpanded ? " expanded" : ""}`,
    });

    const expandBtn =
      pageRows.length > 8
        ? el("button", {
            type: "button",
            className: "btn btn-sm",
            text: state.tableExpanded ? "Compact View" : "Expand Page",
            onClick: () => {
              state.tableExpanded = !state.tableExpanded;
              tableWrap.classList.toggle("expanded", state.tableExpanded);
              expandBtn.textContent = state.tableExpanded ? "Compact View" : "Expand Page";
            },
          })
        : null;

    const rangeLabel =
      sorted.length === 0
        ? "No matching securities"
        : `Showing ${fmtInt(startIdx + 1)}–${fmtInt(endIdx)} of ${fmtInt(sorted.length)} matches — click any row to open fundamentals`;

    resultsCard.appendChild(
      el("div", { className: "table-meta-row" }, [
        el("span", { className: "text-muted", text: rangeLabel }),
        expandBtn,
      ])
    );

    if (sorted.length === 0) {
      resultsCard.appendChild(
        el("div", {
          className: "empty-note",
          text: "No securities match all active criteria. Try relaxing one of the thresholds or clicking Reset All.",
        })
      );
      return;
    }

    const baseCols = [
      { key: "ticker", label: "Ticker", num: false },
      { key: "name", label: "Company", num: false },
      { key: "etf_count", label: "Current ETFs", num: false },
      { key: "sector_group", label: "Sector", num: false },
    ];
    const allCols = [
      ...baseCols,
      ...metricCols.map((c) => ({ ...c, num: true })),
    ];

    const thead = el("thead", {}, [
      el(
        "tr",
        {},
        allCols.map((col) => {
          const isSorted = state.sortKey === col.key;
          const arrow = isSorted ? (state.sortDir === -1 ? " ↓" : " ↑") : "";
          return el("th", {
            className: `${col.num ? "num " : ""}sortable${isSorted ? " sorted" : ""}`,
            text: `${col.label}${arrow}`,
            onClick: () => {
              if (state.sortKey === col.key) {
                state.sortDir *= -1;
              } else {
                state.sortKey = col.key;
                state.sortDir = col.num || col.key === "etf_count" ? -1 : 1;
              }
              state.page = 1;
              refreshResults();
            },
          });
        })
      ),
    ]);

    const tbody = el(
      "tbody",
      {},
      pageRows.map((row) => {
        const activeEtfs = row.current_etfs || [];
        const etfBadges =
          activeEtfs.length > 0
            ? el(
                "div",
                { className: "screener-etf-badges" },
                activeEtfs.map((id) => el("span", { className: "badge badge-blue", text: id }))
              )
            : el("span", { className: "badge badge-slate", text: "Former" });

        return el(
          "tr",
          {
            className: "clickable-row",
            onClick: () => navigate(`#/stock/${encodeURIComponent(row.security_id)}`),
          },
          [
            el("td", {}, [el("span", { className: "ticker-pill", text: row.ticker || row.security_id })]),
            el("td", { className: "company-cell", text: row.name, title: row.name }),
            el("td", {}, [etfBadges]),
            el("td", {
              className: row.sector_group ? "" : "text-muted",
              text: sectorLabel(row.sector_group),
            }),
            ...metricCols.map((col) => {
              const val = col.get(row);
              let cls = "num";
              if (col.key === "share_change_5y" && val !== null && val !== undefined) {
                cls += val < 0 ? " pos-val" : val > 0.05 ? " neg-val" : "";
              }
              return el("td", { className: cls, text: col.fmt(val, row) });
            }),
          ]
        );
      })
    );

    tableWrap.appendChild(el("table", { className: "data-table" }, [thead, tbody]));
    resultsCard.appendChild(tableWrap);

    if (totalPages > 1) {
      resultsCard.appendChild(
        el("div", { className: "screener-pagination" }, [
          el("button", {
            type: "button",
            className: "btn btn-sm",
            text: "← Prev",
            disabled: state.page <= 1,
            onClick: () => {
              state.page = Math.max(1, state.page - 1);
              renderResultsTable(sorted);
            },
          }),
          el("span", {
            className: "text-muted screener-page-indicator",
            text: `Page ${fmtInt(state.page)} of ${fmtInt(totalPages)}`,
          }),
          el("button", {
            type: "button",
            className: "btn btn-sm",
            text: "Next →",
            disabled: state.page >= totalPages,
            onClick: () => {
              state.page = Math.min(totalPages, state.page + 1);
              renderResultsTable(sorted);
            },
          }),
        ])
      );
    }
  }

  function refreshResults() {
    const filtered = filterStocks(stocksList, state.filters);
    const sorted = sortStocks(filtered, state.sortKey, state.sortDir);
    lastMatchedRows = sorted;
    updatePresetHighlight();
    renderActiveChips();
    renderSummaryKpis(sorted);
    renderResultsTable(sorted);
    syncUrl();
  }

  syncControlsFromState();
  refreshResults();
  return null;
}
