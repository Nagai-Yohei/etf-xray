/**
 * ETF View (#/etf/<ETF_ID>)
 * - Reporting-date switcher & summary KPIs
 * - Customizable holdings table (presets + column toggles + sorting + filtering)
 * - Reporting-date additions/removals & full-history turnover chart
 * - Holding tenure map (Canvas heatmap)
 */

import { getEtfHoldings, getEtfSummary, getStocksIndex } from "./api.js";
import {
  baseChartOptions,
  createChart,
  destroyAllCharts,
  fmtAxisPct,
  fmtAxisSignedInt,
  fmtAxisUsdB,
  fmtAxisUsdM,
  PALETTE,
} from "./charts.js";
import {
  clearNode,
  el,
  fmtMultiple,
  fmtPct,
  fmtPctPoints,
  fmtPerShare,
  fmtPrice,
  fmtStreak,
  fmtUsdCompact,
} from "./dom.js";

const STORAGE_KEY_COLS = "etf_xray_visible_cols_v1";

// Customizable metric column definitions
const OPTIONAL_COLUMNS = [
  { key: "price", label: "Price", group: "Valuation", get: (r) => r.price, fmt: (v) => fmtPrice(v) },
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
  { key: "roic", label: "ROIC", group: "Fundamentals", get: (r) => r.m?.roic, fmt: (v) => fmtPct(v, 1) },
  { key: "roe", label: "ROE", group: "Fundamentals", get: (r) => r.m?.roe, fmt: (v) => fmtPct(v, 1) },
  { key: "equity_ratio", label: "Equity Ratio", group: "Fundamentals", get: (r) => r.m?.equity_ratio, fmt: (v) => fmtPct(v, 1) },
  { key: "share_change_5y", label: "5Y Share Δ", group: "Fundamentals", get: (r) => r.m?.share_change_5y, fmt: (v) => fmtPct(v, 1, true) },
  { key: "interest_coverage", label: "Int. Cov.", group: "Fundamentals", get: (r) => r.m?.interest_coverage, fmt: (v) => fmtMultiple(v) },
];

const COLUMN_PRESETS = {
  Overview: ["price", "ttm_yield", "div_streak_years", "div_cagr_5y", "payout_ratio_eps", "pe_ratio", "roic"],
  Dividend: ["ttm_yield", "div_streak_years", "div_cagr_5y", "div_cagr_10y", "payout_ratio_eps", "payout_ratio_fcf", "dps_ttm"],
  Fundamentals: ["revenue_ttm", "gross_margin", "op_margin", "fcf_ex_sbc_margin", "roic", "roe", "share_change_5y"],
  Valuation: ["price", "pe_ratio", "pb_ratio", "ttm_yield", "dps_ttm"],
  All: OPTIONAL_COLUMNS.map((c) => c.key),
};

function loadSavedColumns() {
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

function saveColumns(colSet) {
  try {
    localStorage.setItem(STORAGE_KEY_COLS, JSON.stringify([...colSet]));
  } catch {
    // Ignore
  }
}

function detectActivePreset(colSet) {
  for (const [name, list] of Object.entries(COLUMN_PRESETS)) {
    if (list.length === colSet.size && list.every((k) => colSet.has(k))) {
      return name;
    }
  }
  return null;
}

export async function renderEtfView(root, etfId, initialAsOf, navigate, onDateChange = null) {
  destroyAllCharts();
  clearNode(root);
  root.appendChild(el("div", { className: "loading-box", text: `Loading ${etfId} holdings...` }));

  const [summary, stocksIndex] = await Promise.all([
    getEtfSummary(etfId),
    getStocksIndex(),
  ]);

  const dateList = summary.dates.map((d) => d.as_of);
  let selectedAsOf =
    initialAsOf && dateList.includes(initialAsOf) ? initialAsOf : summary.latest_as_of;

  const state = {
    visibleCols: loadSavedColumns(),
    sortKey: "rank",
    sortDir: "asc",
    filterText: "",
    onlyNew: false,
    tableExpanded: false,
    popoverOpen: false,
    tenureFilter: "current",
    holdingsData: null,
  };

  // Root container layout (Zone 1: Quarter Snapshot / Zone 2: Full History)
  clearNode(root);
  const topHeaderContainer = el("div");

  // Zone 1: Components bound to the selected reporting date
  const snapshotZone = el("section", { className: "zone-block" });
  const snapshotHeader = el("div");
  const kpiContainer = el("div", { className: "kpi-grid" });
  const tableCard = el("section", { className: "card" });
  const changesCard = el("section", { className: "card" });
  snapshotZone.appendChild(snapshotHeader);
  snapshotZone.appendChild(kpiContainer);
  snapshotZone.appendChild(tableCard);
  snapshotZone.appendChild(changesCard);

  // Zone 2: Full-history charts (syncs bidirectionally with Zone 1 via date highlight & clicks)
  const historyZone = el("section", { className: "zone-block" });
  const historyHeader = el("div", { className: "zone-header" }, [
    el("div", { className: "zone-title-wrap" }, [
      el("h2", { className: "zone-title", text: "Full History" }),
      el("span", {
        className: "badge badge-slate",
        text: `${dateList[0]} – ${dateList[dateList.length - 1]} (${dateList.length} quarters)`,
      }),
    ]),
    el("span", {
      className: "zone-hint",
      text: "Selected quarter is highlighted · Click any bar to jump to that quarter",
    }),
  ]);
  const chartsRow = el("div", { className: "charts-row-2" });
  const dividendChartCard = el("section", { className: "card" });
  const turnoverCard = el("section", { className: "card" });
  const tenureCard = el("section", { className: "card" });

  chartsRow.appendChild(dividendChartCard);
  chartsRow.appendChild(turnoverCard);
  historyZone.appendChild(historyHeader);
  historyZone.appendChild(chartsRow);
  historyZone.appendChild(tenureCard);

  root.appendChild(topHeaderContainer);
  root.appendChild(snapshotZone);
  root.appendChild(historyZone);

  const changesByDate = new Map(summary.changes.map((c) => [c.as_of, c]));
  const dateMetaMap = new Map(summary.dates.map((d) => [d.as_of, d]));
  let divChartInst = null;
  let turnChartInst = null;

  function renderTopHeader() {
    clearNode(topHeaderContainer);
    topHeaderContainer.appendChild(
      el("div", { className: "view-header" }, [
        el("div", {}, [
          el("div", { className: "view-title-row" }, [
            el("h1", { className: "view-title", text: `${summary.etf_id}` }),
            el("span", { className: "badge badge-blue", text: summary.issuer }),
          ]),
          el("p", {
            className: "view-subtitle",
            text: `${summary.name} · ${dateList[0]} to ${dateList[dateList.length - 1]} (${dateList.length} quarters)`,
          }),
        ]),
      ])
    );
  }

  async function selectDate(asOf, scrollToSnapshot = false) {
    selectedAsOf = asOf;
    if (typeof onDateChange === "function") {
      onDateChange(selectedAsOf, selectedAsOf === summary.latest_as_of);
    }
    state.holdingsData = await getEtfHoldings(etfId, asOf);
    renderSnapshotHeader();
    renderKpis();
    renderHoldingsCard();
    renderChangesCard();
    updateHistoryHighlights();
    if (scrollToSnapshot) {
      snapshotZone.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }

  function renderSnapshotHeader() {
    clearNode(snapshotHeader);
    const idx = dateList.indexOf(selectedAsOf);

    const dateSelect = el(
      "select",
      {
        className: "select-control",
        ariaLabel: "Select quarter-end date",
        onChange: (e) => selectDate(e.target.value),
      },
      [...dateList].reverse().map((d) =>
        el("option", { value: d, selected: d === selectedAsOf, text: d })
      )
    );

    const prevBtn = el("button", {
      type: "button",
      className: "btn",
      text: "← Prev",
      disabled: idx <= 0,
      onClick: () => {
        if (idx > 0) selectDate(dateList[idx - 1]);
      },
    });
    const nextBtn = el("button", {
      type: "button",
      className: "btn",
      text: "Next →",
      disabled: idx >= dateList.length - 1,
      onClick: () => {
        if (idx < dateList.length - 1) selectDate(dateList[idx + 1]);
      },
    });

    snapshotHeader.appendChild(
      el("div", { className: "zone-header" }, [
        el("div", { className: "zone-title-wrap" }, [
          el("h2", { className: "zone-title", text: "Quarter Snapshot" }),
          el("span", { className: "badge badge-blue", text: `As of ${selectedAsOf}` }),
        ]),
        el("div", { className: "date-nav" }, [prevBtn, dateSelect, nextBtn]),
      ])
    );
  }

  function renderKpis() {
    clearNode(kpiContainer);
    const hd = state.holdingsData;
    const dMeta = dateMetaMap.get(selectedAsOf);
    const ch = changesByDate.get(selectedAsOf) || { added: [], removed: [] };

    const estYield = dMeta?.est_yield ?? null;
    const estDiv = dMeta?.est_annual_dividends_usd ?? null;

    const cards = [
      {
        label: "Total Equity Value",
        value: fmtUsdCompact(hd.total_value_usd),
        sub: `As of ${hd.as_of}`,
      },
      {
        label: "Holdings Count",
        value: String(hd.holdings_count),
        sub: `Top 10 weight: ${fmtPct(
          hd.holdings.slice(0, 10).reduce((acc, x) => acc + x.weight, 0),
          1
        )}`,
      },
      {
        label: "Quarter Turnover",
        value: `+${ch.added.length} / -${ch.removed.length}`,
        sub:
          selectedAsOf === dateList[0]
            ? "Initial quarter in dataset"
            : "Added / Removed vs prior quarter",
      },
      {
        label: "Est. Portfolio Yield",
        value: fmtPct(estYield, 2),
        sub: estDiv != null ? `Est. annual dividends: ${fmtUsdCompact(estDiv)}` : "Estimated annual yield",
      },
    ];

    for (const c of cards) {
      kpiContainer.appendChild(
        el("div", { className: "kpi-card" }, [
          el("div", { className: "kpi-label", text: c.label }),
          el("div", { className: "kpi-value", text: c.value }),
          el("div", { className: "kpi-sub", text: c.sub }),
        ])
      );
    }
  }

  function renderHoldingsCard() {
    clearNode(tableCard);
    const hd = state.holdingsData;
    const activePreset = detectActivePreset(state.visibleCols);
    const activeOptCols = OPTIONAL_COLUMNS.filter((c) => state.visibleCols.has(c.key));

    // Build & filter rows
    const q = state.filterText.trim().toLowerCase();
    const rows = hd.holdings
      .map((h) => ({
        h,
        m: stocksIndex[h.security_id] || null,
        price: h.price_usd,
      }))
      .filter(({ h }) => {
        if (state.onlyNew && !h.is_new) return false;
        if (!q) return true;
        return (
          (h.ticker || "").toLowerCase().includes(q) ||
          (h.name || "").toLowerCase().includes(q)
        );
      });

    // Sort rows
    const optMap = new Map(OPTIONAL_COLUMNS.map((c) => [c.key, c]));
    const dir = state.sortDir === "asc" ? 1 : -1;
    rows.sort((a, b) => {
      let va;
      let vb;
      if (state.sortKey === "rank") {
        va = a.h.rank;
        vb = b.h.rank;
      } else if (state.sortKey === "ticker") {
        va = a.h.ticker || "";
        vb = b.h.ticker || "";
      } else if (state.sortKey === "name") {
        va = a.h.name || "";
        vb = b.h.name || "";
      } else if (state.sortKey === "weight") {
        va = a.h.weight;
        vb = b.h.weight;
      } else if (state.sortKey === "qoq") {
        va = a.h.qoq_weight_diff;
        vb = b.h.qoq_weight_diff;
      } else if (optMap.has(state.sortKey)) {
        const getter = optMap.get(state.sortKey).get;
        va = getter(a);
        vb = getter(b);
      }

      if (va === null || va === undefined) return 1;
      if (vb === null || vb === undefined) return -1;
      if (typeof va === "string" && typeof vb === "string") {
        return va.localeCompare(vb) * dir;
      }
      return (Number(va) - Number(vb)) * dir;
    });

    function toggleSort(key, defaultDir = "desc") {
      if (state.sortKey === key) {
        state.sortDir = state.sortDir === "asc" ? "desc" : "asc";
      } else {
        state.sortKey = key;
        state.sortDir = defaultDir;
      }
      renderHoldingsCard();
    }

    function sortMark(key) {
      if (state.sortKey !== key) return "";
      return state.sortDir === "asc" ? " ▲" : " ▼";
    }

    // Header
    tableCard.appendChild(
      el("div", { className: "card-header" }, [
        el("h2", {
          className: "card-title",
          text: `Holdings (${rows.length} of ${hd.holdings.length}) · ${hd.as_of}`,
        }),
        el("span", {
          className: "text-muted",
          text: "Click any row to open Stock View (annual SEC financials & ETF history)",
        }),
      ])
    );

    // Preset & column customization toolbar
    const presetButtons = Object.keys(COLUMN_PRESETS).map((name) =>
      el("button", {
        type: "button",
        className: `btn btn-sm${activePreset === name ? " btn-active" : ""}`,
        text: name,
        onClick: () => {
          state.visibleCols = new Set(COLUMN_PRESETS[name]);
          saveColumns(state.visibleCols);
          renderHoldingsCard();
        },
      })
    );

    const colPopover = el("div", {
      className: `col-popover${state.popoverOpen ? "" : " hidden"}`,
    });
    for (const groupName of ["Dividend", "Valuation", "Fundamentals"]) {
      colPopover.appendChild(el("div", { className: "col-popover-title", text: groupName }));
      const grid = el("div", { className: "col-check-grid" });
      for (const col of OPTIONAL_COLUMNS.filter((c) => c.group === groupName)) {
        const checkbox = el("input", {
          type: "checkbox",
          checked: state.visibleCols.has(col.key),
          onChange: (e) => {
            if (e.target.checked) {
              state.visibleCols.add(col.key);
            } else {
              state.visibleCols.delete(col.key);
            }
            saveColumns(state.visibleCols);
            renderHoldingsCard();
          },
        });
        grid.appendChild(el("label", { className: "col-check-label" }, [checkbox, col.label]));
      }
      colPopover.appendChild(grid);
    }

    const colMenuWrap = el("div", { className: "col-menu-wrap" }, [
      el("button", {
        type: "button",
        className: `btn btn-sm${state.popoverOpen ? " btn-active" : ""}`,
        text: `Columns (${state.visibleCols.size})`,
        onClick: (e) => {
          e.stopPropagation();
          state.popoverOpen = !state.popoverOpen;
          renderHoldingsCard();
        },
      }),
      colPopover,
    ]);

    const filterInput = el("input", {
      type: "search",
      className: "filter-input",
      placeholder: "Filter holdings...",
      value: state.filterText,
      onInput: (e) => {
        state.filterText = e.target.value;
        renderHoldingsCard();
        // Preserve input focus across re-renders
        const nextInput = tableCard.querySelector(".filter-input");
        if (nextInput) {
          nextInput.focus();
          nextInput.setSelectionRange(nextInput.value.length, nextInput.value.length);
        }
      },
    });

    const newOnlyBtn = el("button", {
      type: "button",
      className: `btn btn-sm${state.onlyNew ? " btn-active" : ""}`,
      text: "New Only",
      onClick: () => {
        state.onlyNew = !state.onlyNew;
        renderHoldingsCard();
      },
    });

    const expandBtn = el("button", {
      type: "button",
      className: "btn btn-sm",
      text: state.tableExpanded ? "Compact View" : "Expand All",
      onClick: () => {
        state.tableExpanded = !state.tableExpanded;
        renderHoldingsCard();
      },
    });

    tableCard.appendChild(
      el("div", { className: "toolbar" }, [
        el("div", { className: "toolbar-group" }, [...presetButtons, colMenuWrap]),
        el("div", { className: "toolbar-group" }, [filterInput, newOnlyBtn, expandBtn]),
      ])
    );

    // Table body
    const theadRow = el("tr", {}, [
      el("th", { className: "num sortable", text: `#${sortMark("rank")}`, onClick: () => toggleSort("rank", "asc") }),
      el("th", { className: "sortable", text: `Ticker${sortMark("ticker")}`, onClick: () => toggleSort("ticker", "asc") }),
      el("th", { className: "sortable", text: `Company${sortMark("name")}`, onClick: () => toggleSort("name", "asc") }),
      el("th", { className: "num sortable", text: `Weight${sortMark("weight")}`, onClick: () => toggleSort("weight", "desc") }),
      el("th", { className: "num sortable", text: `QoQ${sortMark("qoq")}`, onClick: () => toggleSort("qoq", "desc") }),
      ...activeOptCols.map((col) =>
        el("th", {
          className: "num sortable",
          text: `${col.label}${sortMark(col.key)}`,
          onClick: () => toggleSort(col.key, "desc"),
        })
      ),
    ]);

    const tbody = el("tbody");
    if (rows.length === 0) {
      tbody.appendChild(
        el("tr", {}, [
          el("td", {
            colSpan: 5 + activeOptCols.length,
            className: "text-muted",
            text: "No holdings match the current filter.",
          }),
        ])
      );
    } else {
      for (const row of rows) {
        const { h } = row;
        let qoqNode;
        if (h.is_new) {
          qoqNode = el("span", { className: "badge badge-green", text: "NEW" });
        } else if (h.qoq_weight_diff !== null && h.qoq_weight_diff !== undefined) {
          const cls = h.qoq_weight_diff > 0 ? "text-pos" : h.qoq_weight_diff < 0 ? "text-neg" : "text-muted";
          qoqNode = el("span", { className: cls, text: fmtPctPoints(h.qoq_weight_diff, 2) });
        } else {
          qoqNode = el("span", { className: "text-muted", text: "—" });
        }

        const tr = el(
          "tr",
          {
            className: "clickable-row",
            onClick: () => navigate(`#/stock/${encodeURIComponent(h.security_id)}`),
          },
          [
            el("td", { className: "num text-muted", text: String(h.rank) }),
            el("td", {}, [el("span", { className: "ticker-pill", text: h.ticker || h.security_id })]),
            el("td", { className: "company-cell", title: h.name, text: h.name }),
            el("td", { className: "num", text: fmtPct(h.weight, 2) }),
            el("td", { className: "num" }, [qoqNode]),
            ...activeOptCols.map((col) => {
              const val = col.get(row);
              return el("td", { className: "num", text: col.fmt(val, row) });
            }),
          ]
        );
        tbody.appendChild(tr);
      }
    }

    tableCard.appendChild(
      el(
        "div",
        {
          className: `table-scroll holdings-table-scroll${state.tableExpanded ? " expanded" : ""}`,
        },
        [el("table", { className: "data-table" }, [el("thead", {}, [theadRow]), tbody])]
      )
    );
  }

  function renderChangesCard() {
    clearNode(changesCard);
    const ch = changesByDate.get(selectedAsOf) || { added: [], removed: [] };

    changesCard.appendChild(
      el("div", { className: "card-header" }, [
        el("h2", { className: "card-title", text: `Constituent Changes on ${selectedAsOf}` }),
        el("span", {
          className: "text-muted",
          text: `${ch.added.length} Added · ${ch.removed.length} Removed`,
        }),
      ])
    );

    const makeChips = (items, kind) => {
      if (!items.length) {
        return el("p", { className: "text-muted", text: "None in this quarter." });
      }
      return el(
        "div",
        { className: "chip-list" },
        items.map((item) =>
          el("a", {
            href: `#/stock/${encodeURIComponent(item.security_id)}`,
            className: `change-chip ${kind === "add" ? "change-chip-add" : "change-chip-remove"}`,
            title: item.name,
            text: `${item.ticker || item.security_id} (${fmtPct(item.weight, 2)})`,
          })
        )
      );
    };

    changesCard.appendChild(
      el("div", { className: "card-body changes-grid" }, [
        el("div", {}, [
          el("div", { className: "kpi-label", text: `Added (${ch.added.length})` }),
          makeChips(ch.added, "add"),
        ]),
        el("div", {}, [
          el("div", { className: "kpi-label", text: `Removed (${ch.removed.length})` }),
          makeChips(ch.removed, "remove"),
        ]),
      ])
    );
  }

  function barHighlightColors(baseColor, activeColor) {
    const selIdx = dateList.indexOf(selectedAsOf);
    return dateList.map((_, i) => (i === selIdx ? activeColor : baseColor));
  }

  function updateHistoryHighlights() {
    if (divChartInst) {
      divChartInst.data.datasets[0].backgroundColor = barHighlightColors(
        "rgba(5, 150, 105, 0.55)",
        "#047857"
      );
      divChartInst.update("none");
    }
    if (turnChartInst) {
      turnChartInst.data.datasets[0].backgroundColor = barHighlightColors(
        "rgba(5, 150, 105, 0.55)",
        "#047857"
      );
      turnChartInst.data.datasets[1].backgroundColor = barHighlightColors(
        "rgba(225, 29, 72, 0.55)",
        "#be123c"
      );
      turnChartInst.update("none");
    }
    renderTenureMapCard();
  }

  function renderDividendChartCard() {
    clearNode(dividendChartCard);
    const canvas = el("canvas", {
      className: "chart-interactive",
      ariaLabel: "Estimated annual dividends and yield history chart",
    });
    dividendChartCard.appendChild(
      el("div", { className: "card-header" }, [
        el("h2", { className: "card-title", text: "Estimated Annual Dividends & Yield History" }),
        el("span", { className: "text-muted", text: "Portfolio look-through basis" }),
      ])
    );
    dividendChartCard.appendChild(
      el("div", { className: "card-body" }, [el("div", { className: "chart-box" }, [canvas])])
    );

    const labels = summary.dates.map((d) => d.as_of.slice(0, 7));
    const estDivM = summary.dates.map((d) =>
      d.est_annual_dividends_usd != null ? +(d.est_annual_dividends_usd / 1e6).toFixed(1) : null
    );
    const estYieldPct = summary.dates.map((d) =>
      d.est_yield != null ? +(d.est_yield * 100).toFixed(2) : null
    );

    const maxDivM = Math.max(...estDivM.filter((v) => v != null), 0);
    const useBillions = maxDivM >= 1500;
    const divSeries = useBillions
      ? estDivM.map((v) => (v != null ? +(v / 1000).toFixed(2) : null))
      : estDivM;

    const opts = baseChartOptions(useBillions ? fmtAxisUsdB : fmtAxisUsdM, fmtAxisPct);
    opts.onClick = (_ev, elements) => {
      if (elements && elements.length > 0) {
        const idx = elements[0].index;
        if (dateList[idx]) selectDate(dateList[idx], true);
      }
    };

    divChartInst = createChart(canvas, {
      type: "bar",
      data: {
        labels,
        datasets: [
          {
            label: useBillions ? "Est. Annual Dividends ($B)" : "Est. Annual Dividends ($M)",
            data: divSeries,
            backgroundColor: barHighlightColors("rgba(5, 150, 105, 0.55)", "#047857"),
            yAxisID: "y",
            order: 2,
          },
          {
            type: "line",
            label: "Est. Portfolio Yield (%)",
            data: estYieldPct,
            borderColor: PALETTE.blue,
            backgroundColor: PALETTE.blue,
            borderWidth: 2,
            pointRadius: 1.8,
            tension: 0.2,
            yAxisID: "y1",
            order: 1,
          },
        ],
      },
      options: opts,
    });
  }

  function renderTurnoverChartCard() {
    clearNode(turnoverCard);
    const canvas = el("canvas", {
      className: "chart-interactive",
      ariaLabel: "Turnover and AUM history chart",
    });
    turnoverCard.appendChild(
      el("div", { className: "card-header" }, [
        el("h2", { className: "card-title", text: "Historical Reconstitution Turnover & Equity Value" }),
        el("span", { className: "text-muted", text: `${summary.dates.length} quarters` }),
      ])
    );
    turnoverCard.appendChild(
      el("div", { className: "card-body" }, [el("div", { className: "chart-box" }, [canvas])])
    );

    const labels = summary.dates.map((d) => d.as_of.slice(0, 7));
    const added = summary.dates.map((d) => d.added_count);
    const removed = summary.dates.map((d) => -d.removed_count);
    const aumB = summary.dates.map((d) => +(d.total_value_usd / 1e9).toFixed(2));

    const opts = baseChartOptions(fmtAxisSignedInt, fmtAxisUsdB);
    opts.onClick = (_ev, elements) => {
      if (elements && elements.length > 0) {
        const idx = elements[0].index;
        if (dateList[idx]) selectDate(dateList[idx], true);
      }
    };

    turnChartInst = createChart(canvas, {
      type: "bar",
      data: {
        labels,
        datasets: [
          {
            label: "Added",
            data: added,
            backgroundColor: barHighlightColors("rgba(5, 150, 105, 0.55)", "#047857"),
            yAxisID: "y",
            order: 2,
          },
          {
            label: "Removed",
            data: removed,
            backgroundColor: barHighlightColors("rgba(225, 29, 72, 0.55)", "#be123c"),
            yAxisID: "y",
            order: 2,
          },
          {
            type: "line",
            label: "Total Equity Value ($B)",
            data: aumB,
            borderColor: PALETTE.blue,
            backgroundColor: PALETTE.blue,
            borderWidth: 2,
            pointRadius: 1.5,
            tension: 0.2,
            yAxisID: "y1",
            order: 1,
          },
        ],
      },
      options: opts,
    });
  }

  function renderTenureMapCard() {
    clearNode(tenureCard);

    let items = summary.tenure_map;
    if (state.tenureFilter === "current") {
      items = items.filter((x) => x.in_latest);
    } else if (state.tenureFilter === "top120") {
      items = [...items]
        .sort((a, b) => b.quarters_held - a.quarters_held)
        .slice(0, 120);
    }

    const filterBtns = [
      { id: "current", label: `Current (${summary.tenure_map.filter((x) => x.in_latest).length})` },
      { id: "top120", label: "Longest Held (Top 120)" },
      { id: "all", label: `All Historical (${summary.tenure_map.length})` },
    ].map((opt) =>
      el("button", {
        type: "button",
        className: `btn btn-sm${state.tenureFilter === opt.id ? " btn-active" : ""}`,
        text: opt.label,
        onClick: () => {
          state.tenureFilter = opt.id;
          renderTenureMapCard();
        },
      })
    );

    const statusLine = el("div", {
      className: "tenure-status",
      text: "Hover over any row to inspect tenure & quarter; click to open Stock View.",
    });

    const labelWidth = 84;
    const countWidth = 42;
    const cellW = 14;
    const rowH = 15;
    const headerH = 28;
    const totalW = labelWidth + countWidth + dateList.length * cellW + 36;
    const bodyH = items.length * rowH + 10;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);

    // Precompute year start column indices for ticks and faint vertical guides
    const yearStarts = [];
    let lastYear = "";
    for (let i = 0; i < dateList.length; i++) {
      const yr = dateList[i].slice(0, 4);
      if (yr !== lastYear) {
        yearStarts.push({ idx: i, year: yr });
        lastYear = yr;
      }
    }
    const selColIdx = dateList.indexOf(selectedAsOf);

    // 1. Sticky year-header canvas (stays pinned at top during vertical scroll)
    const headerCanvas = el("canvas", {
      className: "tenure-header-canvas",
      ariaLabel: "Holding tenure map year header",
    });
    headerCanvas.width = totalW * dpr;
    headerCanvas.height = headerH * dpr;
    headerCanvas.style.width = `${totalW}px`;
    headerCanvas.style.height = `${headerH}px`;

    const hctx = headerCanvas.getContext("2d");
    hctx.scale(dpr, dpr);
    hctx.fillStyle = "#f8fafc";
    hctx.fillRect(0, 0, totalW, headerH);

    if (selColIdx >= 0) {
      const sx = labelWidth + countWidth + selColIdx * cellW;
      hctx.fillStyle = "rgba(37, 99, 235, 0.16)";
      hctx.fillRect(sx, 0, cellW, headerH);
      hctx.fillStyle = "#2563eb";
      hctx.fillRect(sx, headerH - 2, cellW, 2);
    }

    hctx.font = "700 10px -apple-system, BlinkMacSystemFont, sans-serif";
    hctx.fillStyle = "#475569";
    hctx.fillText("Ticker", 8, 18);
    hctx.fillText("Qtrs", labelWidth + 2, 18);

    hctx.strokeStyle = "rgba(148, 163, 184, 0.45)";
    hctx.lineWidth = 1;
    for (let yi = 0; yi < yearStarts.length; yi++) {
      const ys = yearStarts[yi];
      const x = labelWidth + countWidth + ys.idx * cellW;
      const nextX =
        yi + 1 < yearStarts.length
          ? labelWidth + countWidth + yearStarts[yi + 1].idx * cellW
          : totalW - 4;
      const availW = nextX - x;

      hctx.beginPath();
      hctx.moveTo(x + 0.5, headerH - 6);
      hctx.lineTo(x + 0.5, headerH);
      hctx.stroke();

      hctx.fillStyle = "#475569";
      hctx.font = "700 10px -apple-system, BlinkMacSystemFont, sans-serif";
      const fullW = hctx.measureText(ys.year).width;
      if (availW >= fullW + 4) {
        hctx.fillText(ys.year, x + 2, 18);
      } else {
        // Use compact 2-digit year (e.g. "19") for narrow 1-2 quarter partial years to prevent collisions
        const shortLabel = ys.year.slice(2);
        hctx.font = "700 9px -apple-system, BlinkMacSystemFont, sans-serif";
        const shortW = hctx.measureText(shortLabel).width;
        if (availW >= shortW + 2) {
          hctx.fillText(shortLabel, x + Math.max(1, (availW - shortW) / 2), 18);
        }
      }
    }

    // 2. Scrollable rows body canvas
    const canvas = el("canvas", {
      className: "tenure-canvas",
      ariaLabel: "Constituent holding tenure heatmap",
    });
    canvas.width = totalW * dpr;
    canvas.height = bodyH * dpr;
    canvas.style.width = `${totalW}px`;
    canvas.style.height = `${bodyH}px`;

    const ctx = canvas.getContext("2d");
    ctx.scale(dpr, dpr);
    ctx.fillStyle = "#ffffff";
    ctx.fillRect(0, 0, totalW, bodyH);

    // Draw vertical highlight band for the selected reporting date
    if (selColIdx >= 0) {
      ctx.fillStyle = "rgba(37, 99, 235, 0.12)";
      ctx.fillRect(labelWidth + countWidth + selColIdx * cellW, 0, cellW, bodyH);
    }

    // Draw tenure rows
    for (let r = 0; r < items.length; r++) {
      const item = items[r];
      const y = r * rowH;

      if (r % 2 === 1) {
        ctx.fillStyle = "rgba(248, 250, 252, 0.65)";
        ctx.fillRect(0, y, totalW, rowH);
      }

      ctx.font = "600 11px ui-monospace, SFMono-Regular, Menlo, monospace";
      ctx.fillStyle = item.in_latest ? "#1d4ed8" : "#64748b";
      ctx.fillText(item.ticker || item.security_id, 8, y + 11);

      ctx.font = "500 10px -apple-system, BlinkMacSystemFont, sans-serif";
      ctx.fillStyle = "#475569";
      ctx.fillText(String(item.quarters_held), labelWidth + 6, y + 11);

      ctx.fillStyle = item.in_latest ? "#2563eb" : "#94a3b8";
      for (const dIdx of item.date_indices) {
        const x = labelWidth + countWidth + dIdx * cellW;
        ctx.fillRect(x + 1, y + 2, cellW - 2, rowH - 4);
      }
    }

    // Draw subtle vertical year-boundary guide lines across rows
    ctx.strokeStyle = "rgba(148, 163, 184, 0.28)";
    ctx.lineWidth = 1;
    for (const ys of yearStarts) {
      const x = labelWidth + countWidth + ys.idx * cellW;
      ctx.beginPath();
      ctx.moveTo(x + 0.5, 0);
      ctx.lineTo(x + 0.5, bodyH);
      ctx.stroke();
    }

    // Precompute index Sets for instant hover held/not-held lookup
    const heldSets = items.map((item) => new Set(item.date_indices));

    function hitAtEvent(ev) {
      const rect = canvas.getBoundingClientRect();
      const x = ev.clientX - rect.left;
      const y = ev.clientY - rect.top;
      const idx = Math.floor(y / rowH);
      if (idx < 0 || idx >= items.length) return null;
      const col = Math.floor((x - (labelWidth + countWidth)) / cellW);
      const colIdx = col >= 0 && col < dateList.length ? col : -1;
      return { item: items[idx], heldSet: heldSets[idx], colIdx };
    }

    canvas.addEventListener("mousemove", (ev) => {
      const hit = hitAtEvent(ev);
      if (!hit) {
        statusLine.textContent = "Hover over any row to inspect tenure & quarter; click to open Stock View.";
        return;
      }
      const { item, heldSet, colIdx } = hit;
      const status = item.in_latest
        ? `Current holding (${fmtPct(item.latest_weight, 2)})`
        : "Former holding";
      const cursorInfo =
        colIdx >= 0
          ? ` | Quarter ${dateList[colIdx]}: ${heldSet.has(colIdx) ? "Held" : "Not held"}`
          : "";
      statusLine.textContent = `${item.ticker || item.security_id} — ${item.name} | ${item.quarters_held}/${dateList.length} quarters (${item.first_seen} to ${item.last_seen}) · ${status}${cursorInfo}`;
    });

    canvas.addEventListener("mouseleave", () => {
      statusLine.textContent = "Hover over any row to inspect tenure & quarter; click to open Stock View.";
    });

    canvas.addEventListener("click", (ev) => {
      const hit = hitAtEvent(ev);
      if (hit) {
        navigate(`#/stock/${encodeURIComponent(hit.item.security_id)}`);
      }
    });

    tenureCard.appendChild(
      el("div", { className: "card-header" }, [
        el("h2", { className: "card-title", text: "Holding Tenure Map" }),
        el("div", { className: "toolbar-group" }, filterBtns),
      ])
    );
    tenureCard.appendChild(
      el("div", { className: "card-body" }, [
        el("div", { className: "tenure-canvas-wrap" }, [headerCanvas, canvas]),
        statusLine,
      ])
    );
  }

  // Close column popover when clicking outside
  const closePopoverHandler = (ev) => {
    if (state.popoverOpen && !ev.target.closest(".col-menu-wrap")) {
      state.popoverOpen = false;
      renderHoldingsCard();
    }
  };
  document.addEventListener("click", closePopoverHandler, { once: false });

  renderTopHeader();
  renderDividendChartCard();
  renderTurnoverChartCard();
  await selectDate(selectedAsOf);

  return () => {
    document.removeEventListener("click", closePopoverHandler);
  };
}
