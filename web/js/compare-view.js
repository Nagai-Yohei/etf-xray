/**
 * ETF Compare & Overlap View (#/compare?a=<ETF_A>&b=<ETF_B>)
 * - Pairwise ETF overlap analysis (overlap weight = sum min(wA, wB), shared weight, common/unique holdings)
 * - Top overlapping holdings grouped bar chart + portfolio overlap composition chart
 * - Interactive 3-tab sortable & searchable holdings table (Common / Only in A / Only in B)
 * - Full N x N pairwise overlap heatmap matrix (click any cell to switch pair)
 * - Multi-ETF core holdings table (stocks held across 3+, 4+, or all ETFs)
 */

import { getEtfHoldings } from "./api.js";
import {
  baseChartOptions,
  createChart,
  destroyAllCharts,
  destroyChart,
  fmtAxisPct,
  PALETTE,
} from "./charts.js";
import {
  computeCoreHoldings,
  computeOverlapMatrix,
  computePairOverlap,
} from "./compare-helpers.js";
import {
  clearNode,
  el,
  fmtInt,
  fmtMultiple,
  fmtPct,
  fmtPctPoints,
  fmtStreak,
  fmtUsdCompact,
} from "./dom.js";

function matrixCellBg(overlapWeight, isSelf, isSelected) {
  if (isSelf) return "rgba(148, 163, 184, 0.14)";
  const alpha = Math.max(0.06, Math.min(0.45, overlapWeight * 0.85));
  if (isSelected) return `rgba(37, 99, 235, ${Math.min(0.55, alpha + 0.14).toFixed(2)})`;
  return `rgba(37, 99, 235, ${alpha.toFixed(2)})`;
}

function compareNullable(a, b, dir) {
  const aNull = a === null || a === undefined || Number.isNaN(a);
  const bNull = b === null || b === undefined || Number.isNaN(b);
  if (aNull && bNull) return 0;
  if (aNull) return 1;
  if (bNull) return -1;
  if (typeof a === "string" || typeof b === "string") {
    return String(a).localeCompare(String(b)) * dir;
  }
  return (Number(a) - Number(b)) * dir;
}

export async function renderCompareView(
  root,
  metaData,
  stocksIndex,
  initialA,
  initialB,
  navigate,
  onPairChange = null,
  isStale = () => false
) {
  destroyAllCharts();
  clearNode(root);
  root.appendChild(el("div", { className: "loading-box", text: "Loading latest ETF holdings for overlap analysis..." }));

  const etfs = metaData?.etfs || [];
  if (etfs.length < 2) {
    clearNode(root);
    root.appendChild(el("div", { className: "error-box", text: "At least two ETFs are required for comparison." }));
    return null;
  }

  const etfById = new Map(etfs.map((e) => [e.etf_id, e]));
  const etfIds = etfs.map((e) => e.etf_id);

  // Load latest holdings for all ETFs in parallel (cached in api.js)
  const holdingsEntries = await Promise.all(
    etfs.map(async (etf) => {
      const doc = await getEtfHoldings(etf.etf_id, etf.latest_as_of);
      return [etf.etf_id, doc.holdings || []];
    })
  );
  if (isStale()) return null;

  const holdingsByEtf = Object.fromEntries(holdingsEntries);
  const matrix = computeOverlapMatrix(etfIds, holdingsByEtf);

  // Pick initial pair (default SCHD vs VYM when available)
  const defaultA = etfById.has("SCHD") ? "SCHD" : etfIds[0];
  const defaultB = etfById.has("VYM") && defaultA !== "VYM" ? "VYM" : etfIds.find((id) => id !== defaultA);

  let etfA = initialA && etfById.has(initialA) ? initialA : defaultA;
  let etfB = initialB && etfById.has(initialB) && initialB !== etfA ? initialB : defaultB;
  if (etfA === etfB) {
    etfB = etfIds.find((id) => id !== etfA) || etfIds[0];
  }

  const state = {
    etfA,
    etfB,
    activeTab: "common", // common | onlyA | onlyB
    search: "",
    sortKey: "overlap",
    sortDir: -1,
    tableExpanded: false,
    coreMinEtfs: Math.min(etfIds.length, 4),
    coreExpanded: false,
  };

  let topOverlapChart = null;
  let breakdownChart = null;

  function syncUrlAndBackTarget() {
    const targetHash = `#/compare?a=${encodeURIComponent(state.etfA)}&b=${encodeURIComponent(state.etfB)}`;
    if (window.location.hash !== targetHash) {
      window.history.replaceState(null, "", targetHash);
    }
    document.title = `Compare ${state.etfA} vs ${state.etfB} — ETF X-Ray`;
    if (typeof onPairChange === "function") {
      onPairChange(state.etfA, state.etfB);
    }
  }

  function setPair(newA, newB) {
    if (!etfById.has(newA) || !etfById.has(newB) || newA === newB) return;
    state.etfA = newA;
    state.etfB = newB;
    state.tableExpanded = false;
    if (state.activeTab === "common") {
      state.sortKey = "overlap";
      state.sortDir = -1;
    } else {
      state.sortKey = "weight";
      state.sortDir = -1;
    }
    syncUrlAndBackTarget();
    renderAll();
  }

  clearNode(root);

  // Root layout containers
  const headerSlot = el("div", { className: "compare-header-slot" });
  const kpiSlot = el("div", { className: "kpi-grid compare-kpi-grid" });
  const chartsSlot = el("div", { className: "charts-row-2" });
  const tableCard = el("section", { className: "card" });
  const matrixCard = el("section", { className: "card" });
  const coreCard = el("section", { className: "card" });

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
  root.appendChild(headerSlot);
  root.appendChild(kpiSlot);
  root.appendChild(chartsSlot);
  root.appendChild(tableCard);
  root.appendChild(matrixCard);
  root.appendChild(coreCard);

  function renderHeader() {
    clearNode(headerSlot);
    const metaA = etfById.get(state.etfA);
    const metaB = etfById.get(state.etfB);

    const selA = el(
      "select",
      {
        className: "select-control",
        ariaLabel: "Select ETF A",
        onChange: (e) => {
          const val = e.target.value;
          const nextB = val === state.etfB ? state.etfA : state.etfB;
          setPair(val, nextB);
        },
      },
      etfs.map((etf) =>
        el("option", {
          value: etf.etf_id,
          selected: etf.etf_id === state.etfA,
          text: `${etf.etf_id} — ${etf.name}`,
        })
      )
    );

    const selB = el(
      "select",
      {
        className: "select-control",
        ariaLabel: "Select ETF B",
        onChange: (e) => {
          const val = e.target.value;
          const nextA = val === state.etfA ? state.etfB : state.etfA;
          setPair(nextA, val);
        },
      },
      etfs.map((etf) =>
        el("option", {
          value: etf.etf_id,
          selected: etf.etf_id === state.etfB,
          text: `${etf.etf_id} — ${etf.name}`,
        })
      )
    );

    const swapBtn = el("button", {
      type: "button",
      className: "btn btn-sm",
      text: "⇄ Swap",
      title: "Swap ETF A and ETF B",
      onClick: () => setPair(state.etfB, state.etfA),
    });

    const quickPairs = [
      ["SCHD", "VYM"],
      ["SCHD", "HDV"],
      ["VOO", "DIA"],
      ["VOO", "VYM"],
      ["VYM", "HDV"],
    ].filter(([a, b]) => etfById.has(a) && etfById.has(b));

    headerSlot.appendChild(
      el("div", { className: "page-header" }, [
        el("div", { className: "page-title-block" }, [
          el("h1", { className: "page-title", text: "ETF Overlap & Comparison" }),
          el("p", {
            className: "page-subtitle",
            text:
              `Comparing latest SEC holdings snapshots: ${state.etfA} (${metaA.latest_as_of}) vs ${state.etfB} (${metaB.latest_as_of}). ` +
              "Weight overlap is defined as Σ min(weight A, weight B).",
          }),
        ]),
      ])
    );

    headerSlot.appendChild(
      el("div", { className: "card compare-selector-card" }, [
        el("div", { className: "compare-selector-row" }, [
          el("div", { className: "compare-picker" }, [
            el("span", { className: "compare-picker-label compare-dot-a", text: "ETF A" }),
            selA,
          ]),
          swapBtn,
          el("div", { className: "compare-picker" }, [
            el("span", { className: "compare-picker-label compare-dot-b", text: "ETF B" }),
            selB,
          ]),
        ]),
        quickPairs.length > 0
          ? el("div", { className: "compare-quick-pairs" }, [
              el("span", { className: "text-muted compare-quick-label", text: "Quick pairs:" }),
              ...quickPairs.map(([qa, qb]) => {
                const isActive =
                  (state.etfA === qa && state.etfB === qb) ||
                  (state.etfA === qb && state.etfB === qa);
                return el("button", {
                  type: "button",
                  className: `btn btn-sm${isActive ? " active" : ""}`,
                  text: `${qa} vs ${qb}`,
                  onClick: () => setPair(qa, qb),
                });
              }),
            ])
          : null,
      ])
    );
  }

  function renderKpis(ov) {
    clearNode(kpiSlot);
    const metaA = etfById.get(state.etfA);
    const metaB = etfById.get(state.etfB);

    const cards = [
      {
        label: "Weight Overlap",
        value: fmtPct(ov.overlapWeight, 1),
        sub: "Σ min(weight A, weight B)",
        title: "Institutional portfolio overlap: sum of the smaller weight across all shared holdings.",
      },
      {
        label: "Overlapping Stocks",
        value: fmtInt(ov.commonCount),
        sub: `${fmtPct(ov.countA > 0 ? ov.commonCount / ov.countA : 0, 1)} of ${state.etfA} · ${fmtPct(ov.countB > 0 ? ov.commonCount / ov.countB : 0, 1)} of ${state.etfB}`,
      },
      {
        label: `${state.etfA} Weight in Common`,
        value: fmtPct(ov.sharedWeightA, 1),
        sub: `${fmtPct(ov.uniqueWeightA, 1)} unique to ${state.etfA} (${fmtInt(ov.onlyACount)} stocks)`,
      },
      {
        label: `${state.etfB} Weight in Common`,
        value: fmtPct(ov.sharedWeightB, 1),
        sub: `${fmtPct(ov.uniqueWeightB, 1)} unique to ${state.etfB} (${fmtInt(ov.onlyBCount)} stocks)`,
      },
      {
        label: "Holdings Count",
        value: `${fmtInt(ov.countA)} vs ${fmtInt(ov.countB)}`,
        sub: `${fmtInt(ov.unionCount)} combined unique stocks`,
      },
      {
        label: "Est. Portfolio Yield",
        value: `${fmtPct(metaA.latest_est_yield, 2)} vs ${fmtPct(metaB.latest_est_yield, 2)}`,
        sub: `AUM ${fmtUsdCompact(metaA.latest_total_value_usd)} vs ${fmtUsdCompact(metaB.latest_total_value_usd)}`,
      },
    ];

    for (const c of cards) {
      kpiSlot.appendChild(
        el("div", { className: "kpi-card", title: c.title || null }, [
          el("div", { className: "kpi-label", text: c.label }),
          el("div", { className: "kpi-value", text: c.value }),
          el("div", { className: "kpi-sub", text: c.sub }),
        ])
      );
    }
  }

  function renderCharts(ov) {
    destroyChart(topOverlapChart);
    destroyChart(breakdownChart);
    topOverlapChart = null;
    breakdownChart = null;
    clearNode(chartsSlot);

    // 1. Left chart: Top 15 Overlapping Holdings weight comparison
    const top15 = ov.common.slice(0, 15);
    const leftCanvas = el("canvas");
    const leftCard = el("div", { className: "card" }, [
      el("div", { className: "card-header" }, [
        el("div", {}, [
          el("h2", { className: "card-title", text: `Top Overlapping Holdings (${state.etfA} vs ${state.etfB})` }),
          el("p", {
            className: "card-subtitle",
            text: top15.length > 0 ? `Top ${top15.length} shared constituents ranked by overlap contribution` : "No overlapping holdings between selected ETFs",
          }),
        ]),
      ]),
      top15.length > 0
        ? el("div", { className: "chart-container chart-container-tall" }, [leftCanvas])
        : el("div", { className: "empty-note", text: "These two ETFs have zero common holdings." }),
    ]);

    // 2. Right chart: Portfolio Composition Breakdown (Common vs Unique weight)
    const rightCanvas = el("canvas");
    const rightCard = el("div", { className: "card" }, [
      el("div", { className: "card-header" }, [
        el("div", {}, [
          el("h2", { className: "card-title", text: "Portfolio Weight Composition" }),
          el("p", {
            className: "card-subtitle",
            text: "Share of each ETF's total equity weight held in overlapping vs unique constituents",
          }),
        ]),
      ]),
      el("div", { className: "chart-container" }, [rightCanvas]),
      el("div", { className: "compare-legend-notes" }, [
        el("p", {
          className: "chart-footnote",
          text:
            `Overlap Weight (${fmtPct(ov.overlapWeight, 1)}) represents the identical core allocation shared by a 50/50 comparison; ` +
            `Shared Weight (${fmtPct(ov.sharedWeightA, 1)} in ${state.etfA}, ${fmtPct(ov.sharedWeightB, 1)} in ${state.etfB}) sums each ETF's own weights across shared tickers.`,
        }),
      ]),
    ]);

    chartsSlot.appendChild(leftCard);
    chartsSlot.appendChild(rightCard);

    if (top15.length > 0) {
      const leftOpts = baseChartOptions(fmtAxisPct, null, {
        tooltip: {
          callbacks: {
            label: (ctx) => {
              const raw = ctx.parsed?.x;
              if (raw === null || raw === undefined || !Number.isFinite(raw)) return null;
              return ` ${ctx.dataset.label || ""}: ${fmtAxisPct(raw)}`;
            },
          },
        },
      });
      leftOpts.indexAxis = "y";
      leftOpts.scales = {
        x: {
          grid: { color: PALETTE.grid },
          ticks: { color: PALETTE.text, font: { size: 11 }, callback: (v) => fmtAxisPct(v) },
        },
        y: {
          grid: { display: false },
          ticks: { color: PALETTE.text, font: { size: 11, weight: "600" } },
        },
      };
      topOverlapChart = createChart(leftCanvas, {
        type: "bar",
        data: {
          labels: top15.map((r) => r.ticker),
          datasets: [
            {
              label: `${state.etfA} Weight (%)`,
              data: top15.map((r) => Number((r.weightA * 100).toFixed(2))),
              backgroundColor: PALETTE.blueSoft,
              borderColor: PALETTE.blue,
              borderWidth: 1,
              borderRadius: 3,
            },
            {
              label: `${state.etfB} Weight (%)`,
              data: top15.map((r) => Number((r.weightB * 100).toFixed(2))),
              backgroundColor: PALETTE.emeraldSoft,
              borderColor: PALETTE.emerald,
              borderWidth: 1,
              borderRadius: 3,
            },
          ],
        },
        options: leftOpts,
      });
    }

    const rightOpts = baseChartOptions(fmtAxisPct, null, {
      tooltip: {
        callbacks: {
          label: (ctx) => {
            const raw = ctx.parsed?.x;
            if (raw === null || raw === undefined || !Number.isFinite(raw)) return null;
            return ` ${ctx.dataset.label || ""}: ${fmtAxisPct(raw)}`;
          },
        },
      },
    });
    rightOpts.indexAxis = "y";
    rightOpts.scales = {
      x: {
        stacked: true,
        max: 100,
        grid: { color: PALETTE.grid },
        ticks: { color: PALETTE.text, font: { size: 11 }, callback: (v) => fmtAxisPct(v) },
      },
      y: {
        stacked: true,
        grid: { display: false },
        ticks: { color: PALETTE.text, font: { size: 12, weight: "700" } },
      },
    };
    breakdownChart = createChart(rightCanvas, {
      type: "bar",
      data: {
        labels: [state.etfA, state.etfB],
        datasets: [
          {
            label: "Shared Holdings Weight (%)",
            data: [
              Number((ov.sharedWeightA * 100).toFixed(2)),
              Number((ov.sharedWeightB * 100).toFixed(2)),
            ],
            backgroundColor: PALETTE.blueSoft,
            borderColor: PALETTE.blue,
            borderWidth: 1,
            borderRadius: 4,
          },
          {
            label: "Unique Holdings Weight (%)",
            data: [
              Number((ov.uniqueWeightA * 100).toFixed(2)),
              Number((ov.uniqueWeightB * 100).toFixed(2)),
            ],
            backgroundColor: PALETTE.slateSoft,
            borderColor: PALETTE.slate,
            borderWidth: 1,
            borderRadius: 4,
          },
        ],
      },
      options: rightOpts,
    });
  }

  function renderHoldingsTable(ov) {
    clearNode(tableCard);

    const tabs = [
      { id: "common", label: `Common Holdings (${fmtInt(ov.commonCount)})`, defaultSort: "overlap" },
      { id: "onlyA", label: `Only in ${state.etfA} (${fmtInt(ov.onlyACount)})`, defaultSort: "weight" },
      { id: "onlyB", label: `Only in ${state.etfB} (${fmtInt(ov.onlyBCount)})`, defaultSort: "weight" },
    ];

    const searchInput = el("input", {
      type: "search",
      className: "search-input compare-table-search",
      placeholder: "Filter ticker or company...",
      value: state.search,
      ariaLabel: "Filter comparison table",
      onInput: (e) => {
        state.search = e.target.value;
        renderTableBody();
      },
    });

    tableCard.appendChild(
      el("div", { className: "card-header compare-table-header" }, [
        el("div", { className: "tab-bar" },
          tabs.map((t) =>
            el("button", {
              type: "button",
              className: `btn btn-sm${state.activeTab === t.id ? " active" : ""}`,
              text: t.label,
              onClick: () => {
                state.activeTab = t.id;
                state.sortKey = t.defaultSort;
                state.sortDir = -1;
                state.tableExpanded = false;
                renderHoldingsTable(ov);
              },
            })
          )
        ),
        searchInput,
      ])
    );

    const tableWrap = el("div", {
      className: `table-scroll compare-table-scroll${state.tableExpanded ? " expanded" : ""}`,
    });
    const countNoteText = el("span", { className: "text-muted" });
    const expandBtn = el("button", {
      type: "button",
      className: "btn btn-sm",
      text: state.tableExpanded ? "Compact View" : "Expand All",
      onClick: () => {
        state.tableExpanded = !state.tableExpanded;
        tableWrap.classList.toggle("expanded", state.tableExpanded);
        expandBtn.textContent = state.tableExpanded ? "Compact View" : "Expand All";
      },
    });
    const metaRow = el("div", { className: "table-meta-row" }, [countNoteText, expandBtn]);
    tableCard.appendChild(metaRow);
    tableCard.appendChild(tableWrap);

    const columns =
      state.activeTab === "common"
        ? [
            { key: "ticker", label: "Ticker", num: false, get: (r) => r.ticker },
            { key: "name", label: "Company", num: false, get: (r) => r.name },
            { key: "weightA", label: `${state.etfA} Weight`, num: true, get: (r) => r.weightA, fmt: (v) => fmtPct(v, 2) },
            { key: "weightB", label: `${state.etfB} Weight`, num: true, get: (r) => r.weightB, fmt: (v) => fmtPct(v, 2) },
            { key: "diff", label: `Diff (${state.etfA}−${state.etfB})`, num: true, get: (r) => r.diff, fmt: (v) => fmtPctPoints(v, 2) },
            { key: "overlap", label: "Overlap", num: true, get: (r) => r.overlap, fmt: (v) => fmtPct(v, 2) },
            { key: "ttm_yield", label: "TTM Yield", num: true, get: (r) => r.m?.ttm_yield, fmt: (v) => fmtPct(v, 2) },
            { key: "div_streak_years", label: "Div Streak", num: true, get: (r) => r.m?.div_streak_years, fmt: (v, r) => fmtStreak(v, r.m?.div_streak_open) },
            { key: "pe_ratio", label: "P/E (TTM)", num: true, get: (r) => r.m?.pe_ratio, fmt: (v) => fmtMultiple(v) },
          ]
        : [
            { key: "rank", label: "#", num: true, get: (r) => r.rank, fmt: (v) => fmtInt(v) },
            { key: "ticker", label: "Ticker", num: false, get: (r) => r.ticker },
            { key: "name", label: "Company", num: false, get: (r) => r.name },
            {
              key: "weight",
              label: `${state.activeTab === "onlyA" ? state.etfA : state.etfB} Weight`,
              num: true,
              get: (r) => r.weight,
              fmt: (v) => fmtPct(v, 2),
            },
            { key: "ttm_yield", label: "TTM Yield", num: true, get: (r) => r.m?.ttm_yield, fmt: (v) => fmtPct(v, 2) },
            { key: "div_streak_years", label: "Div Streak", num: true, get: (r) => r.m?.div_streak_years, fmt: (v, r) => fmtStreak(v, r.m?.div_streak_open) },
            { key: "pe_ratio", label: "P/E (TTM)", num: true, get: (r) => r.m?.pe_ratio, fmt: (v) => fmtMultiple(v) },
            { key: "roic", label: "ROIC", num: true, get: (r) => r.m?.roic, fmt: (v) => fmtPct(v, 1) },
          ];

    function renderTableBody() {
      clearNode(tableWrap);
      const sourceRows =
        state.activeTab === "common" ? ov.common : state.activeTab === "onlyA" ? ov.onlyA : ov.onlyB;
      const q = state.search.trim().toLowerCase();
      const filtered = q
        ? sourceRows.filter(
            (r) =>
              (r.ticker || "").toLowerCase().includes(q) ||
              (r.name || "").toLowerCase().includes(q)
          )
        : sourceRows;

      const sortCol = columns.find((c) => c.key === state.sortKey) || columns[0];
      const sorted = [...filtered].sort((a, b) =>
        compareNullable(sortCol.get(a), sortCol.get(b), state.sortDir)
      );

      countNoteText.textContent = `Showing ${fmtInt(sorted.length)} of ${fmtInt(sourceRows.length)} securities — click any row to open fundamentals`;
      expandBtn.style.display = sorted.length > 8 ? "" : "none";
      expandBtn.textContent = state.tableExpanded ? "Compact View" : "Expand All";
      tableWrap.classList.toggle("expanded", state.tableExpanded);

      if (sorted.length === 0) {
        tableWrap.appendChild(el("div", { className: "empty-note", text: "No matching securities." }));
        return;
      }

      const thead = el("thead", {}, [
        el(
          "tr",
          {},
          columns.map((col) => {
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
                  state.sortDir = col.num ? -1 : 1;
                }
                renderTableBody();
              },
            });
          })
        ),
      ]);

      const tbody = el(
        "tbody",
        {},
        sorted.map((row) =>
          el(
            "tr",
            {
              className: "clickable-row",
              onClick: () => navigate(`#/stock/${encodeURIComponent(row.security_id)}`),
            },
            columns.map((col) => {
              const rawVal = col.get(row);
              if (col.key === "ticker") {
                return el("td", {}, [el("span", { className: "ticker-pill", text: row.ticker })]);
              }
              if (col.key === "diff") {
                const cls = rawVal > 0.00005 ? "num pos-val" : rawVal < -0.00005 ? "num neg-val" : "num";
                return el("td", { className: cls, text: col.fmt(rawVal, row) });
              }
              return el("td", {
                className: col.num ? "num" : "",
                text: col.fmt ? col.fmt(rawVal, row) : String(rawVal ?? "—"),
              });
            })
          )
        )
      );

      tableWrap.appendChild(el("table", { className: "data-table" }, [thead, tbody]));
    }

    renderTableBody();
  }

  function renderMatrix() {
    clearNode(matrixCard);
    matrixCard.appendChild(
      el("div", { className: "card-header" }, [
        el("div", {}, [
          el("h2", { className: "card-title", text: `All-ETF Overlap Matrix (${etfIds.length} × ${etfIds.length})` }),
          el("p", {
            className: "card-subtitle",
            text: "Weight overlap (Σ min(wA, wB)) and common stock count across all covered ETFs. Click any cell to compare that pair.",
          }),
        ]),
      ])
    );

    const thead = el("thead", {}, [
      el("tr", {}, [
        el("th", { text: "ETF" }),
        ...etfIds.map((id) => el("th", { className: "num", text: id })),
      ]),
    ]);

    const tbody = el(
      "tbody",
      {},
      etfIds.map((rowId) =>
        el("tr", {}, [
          el("td", {}, [el("span", { className: "ticker-pill", text: rowId })]),
          ...etfIds.map((colId) => {
            const cell = matrix[rowId][colId];
            const isSelected =
              !cell.self &&
              ((state.etfA === rowId && state.etfB === colId) ||
                (state.etfA === colId && state.etfB === rowId));
            const td = el(
              "td",
              {
                className: `num matrix-cell${cell.self ? " matrix-self" : " matrix-clickable"}${isSelected ? " matrix-selected" : ""}`,
                title: cell.self
                  ? `${rowId}: ${cell.commonCount} holdings`
                  : `${rowId} vs ${colId}: ${fmtPct(cell.overlapWeight, 1)} weight overlap (${cell.commonCount} shared stocks) — click to compare`,
                onClick: cell.self
                  ? null
                  : () => {
                      setPair(rowId, colId);
                      window.scrollTo({ top: 0, behavior: "smooth" });
                    },
              },
              [
                el("div", { className: "matrix-pct", text: cell.self ? "—" : fmtPct(cell.overlapWeight, 1) }),
                el("div", {
                  className: "matrix-cnt text-muted",
                  text: cell.self ? `${fmtInt(cell.commonCount)} stocks` : `${fmtInt(cell.commonCount)} shared`,
                }),
              ]
            );
            td.style.backgroundColor = matrixCellBg(cell.overlapWeight, cell.self, isSelected);
            return td;
          }),
        ])
      )
    );

    matrixCard.appendChild(
      el("div", { className: "table-scroll" }, [
        el("table", { className: "data-table compare-matrix-table" }, [thead, tbody]),
      ])
    );
  }

  function renderCoreHoldings() {
    clearNode(coreCard);
    const thresholds = [etfIds.length, Math.max(2, etfIds.length - 1), Math.max(2, etfIds.length - 2)]
      .filter((v, idx, arr) => arr.indexOf(v) === idx)
      .sort((a, b) => b - a);

    const coreList = computeCoreHoldings(etfIds, holdingsByEtf, stocksIndex, state.coreMinEtfs);

    coreCard.appendChild(
      el("div", { className: "card-header compare-table-header" }, [
        el("div", {}, [
          el("h2", { className: "card-title", text: "Multi-ETF Core Holdings" }),
          el("p", {
            className: "card-subtitle",
            text: `Securities held simultaneously across ${state.coreMinEtfs === etfIds.length ? `all ${etfIds.length}` : `${state.coreMinEtfs}+`} covered ETFs (${fmtInt(coreList.length)} stocks)`,
          }),
        ]),
        el(
          "div",
          { className: "tab-bar" },
          thresholds.map((th) =>
            el("button", {
              type: "button",
              className: `btn btn-sm${state.coreMinEtfs === th ? " active" : ""}`,
              text: th === etfIds.length ? `All ${th} ETFs` : `${th}+ ETFs`,
              onClick: () => {
                state.coreMinEtfs = th;
                state.coreExpanded = false;
                renderCoreHoldings();
              },
            })
          )
        ),
      ])
    );

    if (coreList.length === 0) {
      coreCard.appendChild(el("div", { className: "empty-note", text: "No securities match this threshold." }));
      return;
    }

    const coreWrap = el("div", {
      className: `table-scroll compare-table-scroll${state.coreExpanded ? " expanded" : ""}`,
    });

    if (coreList.length > 8) {
      const coreExpandBtn = el("button", {
        type: "button",
        className: "btn btn-sm",
        text: state.coreExpanded ? "Compact View" : "Expand All",
        onClick: () => {
          state.coreExpanded = !state.coreExpanded;
          coreWrap.classList.toggle("expanded", state.coreExpanded);
          coreExpandBtn.textContent = state.coreExpanded ? "Compact View" : "Expand All";
        },
      });
      coreCard.appendChild(
        el("div", { className: "table-meta-row" }, [
          el("span", {
            className: "text-muted",
            text: `Showing ${fmtInt(coreList.length)} securities — scroll inside table or click any row to open fundamentals`,
          }),
          coreExpandBtn,
        ])
      );
    }

    const thead = el("thead", {}, [
      el("tr", {}, [
        el("th", { text: "Ticker" }),
        el("th", { text: "Company" }),
        el("th", { className: "num", text: "ETFs" }),
        ...etfIds.map((id) => el("th", { className: "num", text: id })),
        el("th", { className: "num", text: "TTM Yield" }),
        el("th", { className: "num", text: "Div Streak" }),
        el("th", { className: "num", text: "P/E (TTM)" }),
      ]),
    ]);

    const tbody = el(
      "tbody",
      {},
      coreList.map((row) =>
        el(
          "tr",
          {
            className: "clickable-row",
            onClick: () => navigate(`#/stock/${encodeURIComponent(row.security_id)}`),
          },
          [
            el("td", {}, [el("span", { className: "ticker-pill", text: row.ticker })]),
            el("td", { text: row.name }),
            el("td", { className: "num" }, [
              el("span", { className: "badge badge-blue", text: `${row.etfCount}/${etfIds.length}` }),
            ]),
            ...etfIds.map((id) =>
              el("td", {
                className: row.weights[id] !== undefined ? "num" : "num text-muted",
                text: row.weights[id] !== undefined ? fmtPct(row.weights[id], 2) : "—",
              })
            ),
            el("td", { className: "num", text: fmtPct(row.m?.ttm_yield, 2) }),
            el("td", { className: "num", text: fmtStreak(row.m?.div_streak_years, row.m?.div_streak_open) }),
            el("td", { className: "num", text: fmtMultiple(row.m?.pe_ratio) }),
          ]
        )
      )
    );

    coreWrap.appendChild(el("table", { className: "data-table" }, [thead, tbody]));
    coreCard.appendChild(coreWrap);
  }

  function renderAll() {
    const ov = computePairOverlap(
      holdingsByEtf[state.etfA] || [],
      holdingsByEtf[state.etfB] || [],
      stocksIndex
    );
    renderHeader();
    renderKpis(ov);
    renderCharts(ov);
    renderHoldingsTable(ov);
    renderMatrix();
    renderCoreHoldings();
  }

  syncUrlAndBackTarget();
  renderAll();

  return () => {
    destroyChart(topOverlapChart);
    destroyChart(breakdownChart);
  };
}
