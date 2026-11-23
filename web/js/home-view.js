/**
 * Home View (#/) — ETF X-Ray landing page.
 * - Hero overview with quick search & platform stats
 * - Covered ETF comparison cards with latest metrics & top-5 holdings preview
 * - Multi-ETF consensus stocks & dividend streak leaders
 * - Public data methodology summary
 */

import { destroyAllCharts } from "./charts.js";
import {
  clearNode,
  el,
  fmtMultiple,
  fmtPct,
  fmtStreak,
  fmtUsdCompact,
} from "./dom.js";

function renderHeroSection(meta, stocksList, navigate) {
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

  // Build quick-jump ticker buttons from stocks present in the dataset
  const stockById = new Map(stocksList.map((s) => [s.security_id, s]));
  const sampleIds = ["KO", "MSFT", "AVGO", "JNJ", "PEP", "CVX", "AAPL", "PG"].filter((id) =>
    stockById.has(id)
  );

  // Inline hero search box
  const searchInput = el("input", {
    type: "search",
    className: "home-search-input",
    placeholder: "Search any stock by ticker or company (e.g. KO, AVGO, Merck)...",
    autocomplete: "off",
    "aria-label": "Search stocks from home page",
  });
  const searchDropdown = el("div", {
    className: "search-dropdown hidden",
    role: "listbox",
  });

  function closeDropdown() {
    searchDropdown.classList.add("hidden");
  }

  function renderResults(query) {
    clearNode(searchDropdown);
    const q = query.trim().toLowerCase();
    if (!q) {
      closeDropdown();
      return;
    }
    const matches = stocksList
      .filter(
        (s) =>
          (s.ticker || "").toLowerCase().includes(q) ||
          (s.name || "").toLowerCase().includes(q)
      )
      .sort((a, b) => {
        const at = (a.ticker || "").toLowerCase();
        const bt = (b.ticker || "").toLowerCase();
        const aExact = at === q ? 0 : at.startsWith(q) ? 1 : 2;
        const bExact = bt === q ? 0 : bt.startsWith(q) ? 1 : 2;
        if (aExact !== bExact) return aExact - bExact;
        return (b.current_etfs?.length || 0) - (a.current_etfs?.length || 0);
      })
      .slice(0, 10);

    if (matches.length === 0) {
      searchDropdown.appendChild(
        el("div", { className: "search-item text-muted", text: "No matching securities" })
      );
    } else {
      for (const s of matches) {
        const etfTag =
          s.current_etfs && s.current_etfs.length > 0
            ? el("span", { className: "badge badge-green", text: s.current_etfs.join(", ") })
            : el("span", { className: "badge badge-slate", text: "Former" });
        const yieldTag =
          s.ttm_yield !== null && s.ttm_yield !== undefined
            ? el("span", {
                className: "num text-muted",
                text: `Yield ${fmtPct(s.ttm_yield, 2)}`,
                title: s.latest_price_date
                  ? `TTM dividend yield based on implied price as of ${s.latest_price_date}`
                  : "TTM dividend yield",
              })
            : null;
        searchDropdown.appendChild(
          el(
            "div",
            {
              className: "search-item",
              role: "option",
              onClick: () => {
                searchInput.value = "";
                closeDropdown();
                navigate(`#/stock/${encodeURIComponent(s.security_id)}`);
              },
            },
            [
              el("div", { className: "search-item-main" }, [
                el("span", { className: "ticker-pill", text: s.ticker || s.security_id }),
                el("span", { className: "search-item-name", text: s.name }),
              ]),
              el("div", { className: "toolbar-group" }, [yieldTag, etfTag]),
            ]
          )
        );
      }
    }
    searchDropdown.classList.remove("hidden");
  }

  searchInput.addEventListener("input", (e) => renderResults(e.target.value));
  searchInput.addEventListener("focus", (e) => {
    if (e.target.value.trim()) renderResults(e.target.value);
  });
  searchInput.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      closeDropdown();
      searchInput.blur();
    } else if (e.key === "Enter") {
      const first = searchDropdown.querySelector(".search-item[role='option']");
      if (first) first.click();
    }
  });

  const searchBox = el("div", { className: "home-search-wrap" }, [searchInput, searchDropdown]);
  const handleOutsideClick = (e) => {
    if (!searchBox.contains(e.target)) {
      closeDropdown();
    }
  };
  document.addEventListener("click", handleOutsideClick);

  const quickChips = el(
    "div",
    { className: "home-quick-row" },
    [
      el("span", { className: "home-quick-label", text: "Quick jump:" }),
      ...sampleIds.map((id) =>
        el("a", {
          href: `#/stock/${encodeURIComponent(id)}`,
          className: "home-quick-chip",
          text: id,
        })
      ),
    ]
  );

  const statsStrip = el("div", { className: "home-stats-grid" }, [
    el("div", { className: "home-stat" }, [
      el("div", { className: "home-stat-value", text: String(etfs.length) }),
      el("div", { className: "home-stat-label", text: "Covered ETFs" }),
    ]),
    el("div", { className: "home-stat" }, [
      el("div", { className: "home-stat-value", text: totalQuarters.toLocaleString("en-US") }),
      el("div", { className: "home-stat-label", text: "Quarterly Filings" }),
    ]),
    el("div", { className: "home-stat" }, [
      el("div", { className: "home-stat-value", text: stocksList.length.toLocaleString("en-US") }),
      el("div", { className: "home-stat-label", text: "Tracked Securities" }),
    ]),
    el("div", { className: "home-stat" }, [
      el("div", {
        className: "home-stat-value",
        text: firstYear && latestYear ? `${firstYear}–${latestYear}` : "—",
      }),
      el("div", { className: "home-stat-label", text: "Historical Coverage" }),
    ]),
  ]);

  const heroEl = el("section", { className: "home-hero" }, [
    el("div", { className: "home-hero-top" }, [
      el("h1", {
        className: "home-hero-title",
        text: "U.S. ETF Holdings History & Stock Fundamentals",
      }),
      el("p", {
        className: "home-hero-desc",
        text:
          "Explore historical ETF constituents, quarterly additions and removals, holding tenure heatmaps, " +
          "and split-adjusted 10-K/10-Q fundamentals and dividend streaks.",
      }),
    ]),
    searchBox,
    quickChips,
    statsStrip,
  ]);

  return {
    heroEl,
    cleanup: () => document.removeEventListener("click", handleOutsideClick),
  };
}

function renderEtfCardsSection(meta, navigate) {
  const etfs = meta.etfs || [];
  const cards = etfs.map((etf) => {
    const spanText =
      etf.first_as_of && etf.latest_as_of
        ? `${etf.first_as_of.slice(0, 4)}–${etf.latest_as_of.slice(0, 4)}`
        : "—";

    const topHoldings = Array.isArray(etf.top_holdings) ? etf.top_holdings : [];
    const maxWeight = topHoldings.reduce((m, h) => Math.max(m, h.weight || 0), 0.01);
    const topRows = topHoldings.map((h) => {
      const ratio = Math.min(1, Math.max(0.05, (h.weight || 0) / maxWeight));
      const prog = el("progress", {
        className: "etf-top-progress",
        max: "100",
        value: String(Math.round(ratio * 100)),
        ariaLabel: `${h.ticker || h.security_id} weight ${fmtPct(h.weight, 2)}`,
      });
      return el(
        "a",
        {
          href: `#/stock/${encodeURIComponent(h.security_id)}`,
          className: "etf-top-row",
          onClick: (e) => {
            e.stopPropagation();
          },
        },
        [
          el("span", { className: "ticker-pill", text: h.ticker || h.security_id }),
          el("span", { className: "etf-top-name", text: h.name, title: h.name }),
          prog,
          el("span", { className: "etf-top-weight num", text: fmtPct(h.weight, 2) }),
        ]
      );
    });

    const card = el(
      "article",
      {
        className: "etf-card",
        onClick: () => navigate(`#/etf/${encodeURIComponent(etf.etf_id)}`),
      },
      [
        el("div", { className: "etf-card-head" }, [
          el("div", { className: "etf-card-id-row" }, [
            el("span", { className: "etf-card-ticker", text: etf.etf_id }),
            el("span", { className: "badge badge-slate", text: etf.issuer }),
          ]),
          el("h3", { className: "etf-card-name", text: etf.name }),
        ]),
        el("div", { className: "etf-card-metrics" }, [
          el("div", { className: "etf-mini-metric" }, [
            el("div", { className: "etf-mini-label", text: "Est. Yield" }),
            el("div", {
              className: "etf-mini-value text-pos",
              text: fmtPct(etf.latest_est_yield, 2),
            }),
          ]),
          el("div", { className: "etf-mini-metric" }, [
            el("div", { className: "etf-mini-label", text: "Holdings" }),
            el("div", {
              className: "etf-mini-value",
              text: String(etf.latest_holdings_count ?? "—"),
            }),
          ]),
          el("div", { className: "etf-mini-metric" }, [
            el("div", { className: "etf-mini-label", text: "Equity Value" }),
            el("div", {
              className: "etf-mini-value",
              text: fmtUsdCompact(etf.latest_total_value_usd),
            }),
          ]),
          el("div", { className: "etf-mini-metric" }, [
            el("div", { className: "etf-mini-label", text: "History" }),
            el("div", {
              className: "etf-mini-value",
              text: `${spanText} (${etf.date_count}Q)`,
            }),
          ]),
        ]),
        topRows.length > 0
          ? el("div", { className: "etf-top-list" }, [
              el("div", { className: "etf-top-heading", text: "Top 5 Holdings" }),
              ...topRows,
            ])
          : null,
        el("div", { className: "etf-card-footer" }, [
          el("span", { className: "text-muted", text: `Latest filing: ${etf.latest_as_of}` }),
          el("span", { className: "etf-card-cta", text: "Open ETF View →" }),
        ]),
      ]
    );
    return card;
  });

  return el("section", { className: "home-section" }, [
    el("div", { className: "home-section-head" }, [
      el("h2", { className: "home-section-title", text: "Covered ETFs" }),
      el("span", {
        className: "home-section-sub",
        text: "Select an ETF to explore historical constituents, rebalance turnover, and holding tenure. Latest reporting dates differ by fund (fiscal quarter-ends).",
      }),
    ]),
    el("div", { className: "etf-card-grid" }, cards),
  ]);
}

function renderHighlightsSection(stocksList, navigate) {
  const activeStocks = stocksList.filter((s) => s.current_etfs && s.current_etfs.length > 0);

  // 1. Widely held across multiple covered ETFs
  const multiEtf = [...activeStocks]
    .filter((s) => s.current_etfs.length >= 3)
    .sort((a, b) => {
      if (b.current_etfs.length !== a.current_etfs.length) {
        return b.current_etfs.length - a.current_etfs.length;
      }
      return (b.revenue_ttm || 0) - (a.revenue_ttm || 0);
    })
    .slice(0, 8);

  // 2. Longest dividend non-cut streaks among currently held stocks
  const divLeaders = [...activeStocks]
    .filter((s) => s.div_streak_years && s.div_streak_years >= 10 && s.ttm_yield && s.ttm_yield > 0)
    .sort((a, b) => {
      if ((b.div_streak_years || 0) !== (a.div_streak_years || 0)) {
        return (b.div_streak_years || 0) - (a.div_streak_years || 0);
      }
      if ((b.current_etfs?.length || 0) !== (a.current_etfs?.length || 0)) {
        return (b.current_etfs?.length || 0) - (a.current_etfs?.length || 0);
      }
      return (b.div_cagr_5y || -999) - (a.div_cagr_5y || -999);
    })
    .slice(0, 8);

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
            el("td", {}, [
              el(
                "div",
                { className: "home-etf-badges" },
                s.current_etfs.map((eid) => el("span", { className: "badge badge-blue", text: eid }))
              ),
            ]),
            el("td", { className: "num", text: fmtPct(s.ttm_yield, 2) }),
            el("td", { className: "num", text: fmtMultiple(s.pe_ratio) }),
          ]
        )
      )
    );

    return el("div", { className: "card home-highlight-card" }, [
      el("div", { className: "card-header" }, [
        el("h3", { className: "card-title", text: "Held Across Multiple ETFs" }),
        el("span", { className: "badge badge-blue", text: "3+ ETFs" }),
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
        el("h3", { className: "card-title", text: "Long Dividend Streaks" }),
        el("span", { className: "badge badge-green", text: "SEC XBRL" }),
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
    el("div", { className: "home-section-head" }, [
      el("h2", { className: "home-section-title", text: "Notable Securities" }),
      el("span", {
        className: "home-section-sub",
        text: "Click any stock to open full 10-K/10-Q charts, balance sheet trends, and ETF ownership history.",
      }),
    ]),
    el("div", { className: "home-highlights-grid" }, [buildMultiTable(), buildDivTable()]),
  ]);
}

function renderMethodologySection() {
  const items = [
    {
      title: "SEC EDGAR Holdings",
      body:
        "Common equity long positions extracted from N-PORT-P XML (2019–present) and legacy " +
        "N-Q / N-CSR / N-CSRS filings (2011–2019), reconciled against reported common stock totals.",
    },
    {
      title: "Split-Adjusted XBRL",
      body:
        "Annual 10-K and quarterly 10-Q fundamentals from SEC CompanyFacts with chained split-factor " +
        "inference across comparative filings and same-fiscal-year payout ratios.",
    },
    {
      title: "Implied Quarter-End Prices",
      body:
        "Zero third-party market-data APIs. Quarter-end share prices are derived directly from " +
        "reported ETF holding market values divided by share counts (Value ÷ Shares).",
    },
  ];

  return el("section", { className: "home-section" }, [
    el("div", { className: "home-method-grid" }, [
      ...items.map((item) =>
        el("div", { className: "home-method-card" }, [
          el("h3", { className: "home-method-title", text: item.title }),
          el("p", { className: "home-method-body", text: item.body }),
        ])
      ),
    ]),
  ]);
}

export async function renderHomeView(rootEl, meta, stocksList, navigate) {
  destroyAllCharts();
  clearNode(rootEl);

  const { heroEl, cleanup } = renderHeroSection(meta, stocksList, navigate);
  rootEl.appendChild(heroEl);
  rootEl.appendChild(renderEtfCardsSection(meta, navigate));
  rootEl.appendChild(renderHighlightsSection(stocksList, navigate));
  rootEl.appendChild(renderMethodologySection());

  return cleanup;
}
