/**
 * Home View (#/) — Minimalist U.S. ETF directory and notable securities overview.
 * Renders directly from meta.json without requiring the full active metrics index.
 */

import { destroyAllCharts } from "./charts.js";
import {
  clearNode,
  el,
  fmtInt,
  fmtMultiple,
  fmtPct,
  fmtStreak,
  fmtUsdCompact,
} from "./dom.js";

function renderHeader(meta) {
  const etfs = meta.etfs || [];
  const totalQuarters = etfs.reduce((sum, e) => sum + (e.date_count || 0), 0);
  const firstYear = etfs.reduce((min, e) => {
    const y = e.first_as_of ? e.first_as_of.slice(0, 4) : null;
    return y && (!min || y < min) ? y : min;
  }, null);
  const latestYear = etfs.reduce((max, e) => {
    const y = e.latest_as_of ? e.latest_as_of.slice(0, 4) : null;
    return y && (!max || y > max) ? y : max;
  }, null);

  const span = firstYear && latestYear ? `${firstYear}–${latestYear}` : "—";
  const tracked = meta.stock_count != null ? fmtInt(meta.stock_count) : "—";

  return el("header", { className: "home-header" }, [
    el("h1", { className: "home-title", text: "U.S. ETF Holdings & SEC Fundamentals" }),
    el("p", {
      className: "home-meta-line",
      text: `${etfs.length} ETFs · ${fmtInt(totalQuarters)} SEC filings · ${tracked} tracked securities · ${span}`,
    }),
  ]);
}

function renderEtfDirectory(meta, navigate) {
  const etfs = [...(meta.etfs || [])];
  const section = el("section", { className: "home-section" });
  const card = el("div", { className: "card" });
  const tableWrap = el("div", { className: "table-scroll" });

  const state = {
    sortKey: "etf_id",
    sortDir: 1,
  };

  const columns = [
    { key: "etf_id", label: "ETF", num: false, get: (e) => e.etf_id },
    { key: "name", label: "Fund", num: false, get: (e) => e.name },
    { key: "issuer", label: "Issuer", num: false, get: (e) => e.issuer },
    { key: "latest_est_yield", label: "Est. Yield", num: true, get: (e) => e.latest_est_yield },
    { key: "latest_holdings_count", label: "Holdings", num: true, get: (e) => e.latest_holdings_count },
    { key: "latest_total_value_usd", label: "Equity Value", num: true, get: (e) => e.latest_total_value_usd },
    { key: "top", label: "Top Holdings", num: false, sortable: false },
    { key: "first_as_of", label: "Coverage", num: true, get: (e) => e.date_count },
    { key: "latest_as_of", label: "Latest Filing", num: true, get: (e) => e.latest_as_of },
  ];

  function renderTable() {
    clearNode(tableWrap);
    const sortCol = columns.find((c) => c.key === state.sortKey) || columns[0];
    const sorted = [...etfs].sort((a, b) => {
      const va = sortCol.get ? sortCol.get(a) : null;
      const vb = sortCol.get ? sortCol.get(b) : null;
      if (va == null && vb == null) return 0;
      if (va == null) return 1;
      if (vb == null) return -1;
      if (typeof va === "string" && typeof vb === "string") {
        return va.localeCompare(vb) * state.sortDir;
      }
      return (Number(va) - Number(vb)) * state.sortDir;
    });

    const thead = el("thead", {}, [
      el(
        "tr",
        {},
        columns.map((col) => {
          if (col.sortable === false) {
            return el("th", { text: col.label });
          }
          const isSorted = state.sortKey === col.key;
          const arrow = isSorted ? (state.sortDir === 1 ? " ↑" : " ↓") : "";
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
              renderTable();
            },
          });
        })
      ),
    ]);

    const tbody = el(
      "tbody",
      {},
      sorted.map((etf) => {
        const spanText =
          etf.first_as_of && etf.latest_as_of
            ? `${etf.first_as_of.slice(0, 4)}–${etf.latest_as_of.slice(0, 4)} (${etf.date_count}Q)`
            : "—";

        const topList = Array.isArray(etf.top_holdings) ? etf.top_holdings.slice(0, 3) : [];
        const topNodes = [];
        topList.forEach((h, idx) => {
          if (idx > 0) topNodes.push(el("span", { className: "text-subtle", text: " · " }));
          topNodes.push(
            el("a", {
              href: `#/stock/${encodeURIComponent(h.security_id)}`,
              className: "inline-ticker-link",
              title: `${h.name} (${fmtPct(h.weight, 2)})`,
              text: `${h.ticker || h.security_id} ${fmtPct(h.weight, 1)}`,
              onClick: (e) => e.stopPropagation(),
            })
          );
        });

        return el(
          "tr",
          {
            className: "clickable-row",
            onClick: () => navigate(`#/etf/${encodeURIComponent(etf.etf_id)}`),
          },
          [
            el("td", {}, [el("span", { className: "ticker-pill", text: etf.etf_id })]),
            el("td", { className: "company-cell", text: etf.name, title: etf.name }),
            el("td", { className: "text-muted", text: etf.issuer }),
            el("td", { className: "num", text: fmtPct(etf.latest_est_yield, 2) }),
            el("td", { className: "num", text: fmtInt(etf.latest_holdings_count) }),
            el("td", { className: "num", text: fmtUsdCompact(etf.latest_total_value_usd) }),
            el("td", { className: "top-holdings-cell" }, topNodes.length > 0 ? topNodes : "—"),
            el("td", { className: "num text-muted", text: spanText }),
            el("td", { className: "num text-muted", text: etf.latest_as_of || "—" }),
          ]
        );
      })
    );

    tableWrap.appendChild(el("table", { className: "data-table" }, [thead, tbody]));
  }

  card.appendChild(
    el("div", { className: "card-header" }, [
      el("div", {}, [
        el("h2", { className: "card-title", text: "Covered ETFs" }),
        el("p", {
          className: "card-subtitle",
          text: "Click any ETF to view historical holdings, turnover, and holding tenure. Reporting dates follow each fund's fiscal quarter-end.",
        }),
      ]),
      el("div", { className: "toolbar-group" }, [
        el("a", {
          href: "#/compare",
          className: "btn btn-sm",
          text: "Compare Overlap",
          onClick: (e) => {
            e.preventDefault();
            navigate("#/compare");
          },
        }),
        el("a", {
          href: "#/screener",
          className: "btn btn-sm",
          text: "Stock Screener",
          onClick: (e) => {
            e.preventDefault();
            navigate("#/screener");
          },
        }),
      ]),
    ])
  );
  card.appendChild(tableWrap);
  renderTable();
  section.appendChild(card);
  return section;
}

function renderHighlightsSection(meta, navigate) {
  const highlights = meta.highlights || {};
  const multiEtf = Array.isArray(highlights.multi_etf) ? highlights.multi_etf : [];
  const divLeaders = Array.isArray(highlights.div_leaders) ? highlights.div_leaders : [];

  if (multiEtf.length === 0 && divLeaders.length === 0) {
    return null;
  }

  function buildMultiTable() {
    const tbody = el(
      "tbody",
      {},
      multiEtf.map((s) =>
        el(
          "tr",
          {
            className: "clickable-row",
            onClick: () => navigate(`#/stock/${encodeURIComponent(s.security_id)}`),
          },
          [
            el("td", {}, [el("span", { className: "ticker-pill", text: s.ticker || s.security_id })]),
            el("td", { className: "company-cell", text: s.name, title: s.name }),
            el("td", {
              className: "etf-list-cell text-muted",
              title: (s.current_etfs || []).join(", "),
              text: `${(s.current_etfs || []).length} (${(s.current_etfs || []).slice(0, 4).join(", ")}${
                (s.current_etfs || []).length > 4 ? "…" : ""
              })`,
            }),
            el("td", { className: "num", text: fmtPct(s.ttm_yield, 2) }),
            el("td", { className: "num", text: fmtMultiple(s.pe_ratio) }),
          ]
        )
      )
    );

    return el("div", { className: "card home-highlight-card" }, [
      el("div", { className: "card-header" }, [
        el("h3", { className: "card-title", text: "Widely Held Across ETFs" }),
        el("span", { className: "text-muted", text: "3+ funds" }),
      ]),
      el("div", { className: "table-scroll" }, [
        el("table", { className: "data-table" }, [
          el("thead", {}, [
            el("tr", {}, [
              el("th", { text: "Ticker" }),
              el("th", { text: "Company" }),
              el("th", { text: "ETFs" }),
              el("th", { className: "num", text: "Yield" }),
              el("th", { className: "num", text: "P/E" }),
            ]),
          ]),
          tbody,
        ]),
      ]),
    ]);
  }

  function buildDivTable() {
    const tbody = el(
      "tbody",
      {},
      divLeaders.map((s) =>
        el(
          "tr",
          {
            className: "clickable-row",
            onClick: () => navigate(`#/stock/${encodeURIComponent(s.security_id)}`),
          },
          [
            el("td", {}, [el("span", { className: "ticker-pill", text: s.ticker || s.security_id })]),
            el("td", { className: "company-cell", text: s.name, title: s.name }),
            el("td", { className: "num", text: fmtStreak(s.div_streak_years, s.div_streak_open) }),
            el("td", { className: "num", text: fmtPct(s.ttm_yield, 2) }),
            el("td", { className: "num", text: fmtPct(s.div_cagr_5y, 1, true) }),
          ]
        )
      )
    );

    return el("div", { className: "card home-highlight-card" }, [
      el("div", { className: "card-header" }, [
        el("h3", { className: "card-title", text: "Longest Dividend Streaks" }),
        el("span", { className: "text-muted", text: "SEC XBRL" }),
      ]),
      el("div", { className: "table-scroll" }, [
        el("table", { className: "data-table" }, [
          el("thead", {}, [
            el("tr", {}, [
              el("th", { text: "Ticker" }),
              el("th", { text: "Company" }),
              el("th", { className: "num", text: "Streak" }),
              el("th", { className: "num", text: "Yield" }),
              el("th", { className: "num", text: "5Y CAGR" }),
            ]),
          ]),
          tbody,
        ]),
      ]),
    ]);
  }

  return el("section", { className: "home-section" }, [
    el("div", { className: "home-highlights-grid" }, [buildMultiTable(), buildDivTable()]),
  ]);
}

export async function renderHomeView(rootEl, meta, _unusedList, navigate) {
  destroyAllCharts();
  clearNode(rootEl);

  rootEl.appendChild(renderHeader(meta));
  rootEl.appendChild(renderEtfDirectory(meta, navigate));
  const highlights = renderHighlightsSection(meta, navigate);
  if (highlights) {
    rootEl.appendChild(highlights);
  }
  return null;
}
