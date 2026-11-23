/**
 * Stock View (#/stock/<SECURITY_ID>) — バフェット・コード風の年次・四半期分析ビュー
 * - ヘッダー & サマリー KPI（利回り・PER/PBR・配当・粗利率/営業or税引前利益率・ROIC・株式数増減・財務健全性）
 * - Earnings (PL) / Cash Flow & Return / Balance Sheet & Share Count / Dividends / Quarterly (10-Q) / ETF Ownership
 * - データが存在しない系列は自動非表示、Op. Income 非報告企業（MRK/CVX/銀行等）は Pretax Income を自動表示
 */

import { getStockDetail } from "./api.js";
import {
  baseChartOptions,
  createChart,
  destroyAllCharts,
  destroyChart,
  fmtAxisNum,
  fmtAxisPct,
  fmtAxisUsd,
  fmtAxisUsdB,
  PALETTE,
} from "./charts.js";
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

function effectiveDps(row) {
  if (row.dps_declared !== null && row.dps_declared !== undefined) return row.dps_declared;
  if (row.dps_paid !== null && row.dps_paid !== undefined) return row.dps_paid;
  const sh = row.shares_diluted || row.shares_outstanding;
  if (row.dividends_paid !== null && row.dividends_paid !== undefined && sh > 0) {
    return row.dividends_paid / sh;
  }
  return null;
}

function hasAnyValue(arr) {
  return Array.isArray(arr) && arr.some((v) => v !== null && v !== undefined && Number.isFinite(Number(v)));
}

function fmtSharesM(val) {
  if (val === null || val === undefined || Number.isNaN(Number(val))) return "—";
  return `${(Number(val) / 1e6).toFixed(1)}M`;
}

function calcAnnualRoic(r) {
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

function calcInterestCoverage(r) {
  const ebit =
    r.operating_income != null
      ? r.operating_income
      : r.pretax_income != null
        ? r.pretax_income + (r.interest_expense || 0)
        : null;
  if (ebit == null || !(r.interest_expense > 0)) return null;
  return ebit / r.interest_expense;
}

function buildTable(headers, rows, compact = false) {
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

export async function renderStockView(root, securityId, fallbackEtfId) {
  destroyAllCharts();
  clearNode(root);
  root.appendChild(el("div", { className: "loading-box", text: `Loading ${securityId}...` }));

  const detail = await getStockDetail(securityId);
  const m = detail.metrics || {};
  const ttm = detail.ttm || {};
  const annual = detail.annual || [];
  const annualDesc = [...annual].reverse();
  const quarterly = detail.quarterly || [];

  const etfIds = Object.keys(detail.etf_history || {});
  const primaryEtf = detail.current_etfs?.[0] || etfIds[0] || fallbackEtfId || "SCHD";

  // Op. Income 非報告企業（製薬・エネルギー・金融等）は Pretax Income を緑系列に自動適用
  const hasOpIncome = hasAnyValue(annual.map((r) => r.operating_income));
  const profitLabel = hasOpIncome ? "Op. Income" : "Pretax Income";
  const profitMarginLabel = hasOpIncome ? "Op. Margin (%)" : "Pretax Margin (%)";
  const getProfit = (r) => (hasOpIncome ? r.operating_income : r.pretax_income);

  const latestAnn = annual.length > 0 ? annual[annual.length - 1] : null;
  const latestProfitMargin =
    latestAnn && getProfit(latestAnn) != null && latestAnn.revenue > 0
      ? getProfit(latestAnn) / latestAnn.revenue
      : null;

  const state = {
    viewMode: "both", // both | charts | tables
    activeTab: "all", // all | pl | cf | bs | div | qtr | etf
    plSeries: {
      revenue: true,
      gross: false,
      profit: true,
      net: true,
      rd: false,
      sga: false,
    },
    cfSeries: {
      ocf: true,
      capex: true,
      fcf: true,
      adjFcf: false,
      icf: false,
      fin: false,
    },
    returnMode: "stacked", // stacked | total | dividends | buybacks
    bsSeries: {
      assets: true,
      equity: true,
      debt: true,
      cash: true,
      netDebt: false,
      goodwill: false,
      eqRatio: true,
    },
    bsRightMode: "shares", // shares | health
  };

  clearNode(root);

  // 1. 戻るリンク
  root.appendChild(
    el("div", { className: "back-row" }, [
      el("a", {
        href: `#/etf/${encodeURIComponent(primaryEtf)}`,
        className: "back-link",
        text: `← Back to ${primaryEtf}`,
      }),
    ])
  );

  // 2. 銘柄ヘッダー
  const etfBadges = etfIds.map((eid) => {
    const hist = detail.etf_history[eid] || [];
    const latest = hist[hist.length - 1];
    const isCurrent = (detail.current_etfs || []).includes(eid);
    const label =
      isCurrent && latest
        ? `${eid} #${latest.rank} · ${fmtPct(latest.weight, 2)}`
        : `Former ${eid} (${hist.length} qtrs)`;
    return el("a", {
      href: `#/etf/${encodeURIComponent(eid)}`,
      className: `badge ${isCurrent ? "badge-green" : "badge-slate"}`,
      text: label,
    });
  });

  root.appendChild(
    el("div", { className: "view-header" }, [
      el("div", {}, [
        el("div", { className: "view-title-row" }, [
          el("h1", { className: "view-title", text: detail.ticker || detail.security_id }),
          ...etfBadges,
        ]),
        el("p", {
          className: "view-subtitle",
          text: detail.name,
        }),
      ]),
      el("div", { className: "stock-price-box" }, [
        el("div", { className: "stock-price-val", text: fmtPrice(m.latest_price) }),
        el("div", {
          className: "stock-price-date",
          text: m.latest_price_date
            ? `Implied price as of ${m.latest_price_date}`
            : "Price unavailable",
        }),
      ]),
    ])
  );

  // 3. サマリー KPI カード（8枚）
  const kpis = [
    {
      label: "TTM Dividend Yield",
      value: fmtPct(m.ttm_yield, 2),
      sub: `TTM DPS: ${fmtPerShare(ttm.dps_ttm)}`,
    },
    {
      label: "P/E (TTM) / P/B",
      value: `${fmtMultiple(m.pe_ratio)} / ${fmtMultiple(m.pb_ratio)}`,
      sub: `TTM EPS: ${fmtPerShare(ttm.eps_ttm)}`,
    },
    {
      label: "Div Streak / 5Y CAGR",
      value: `${fmtStreak(m.div_streak_years, m.div_streak_open)}`,
      sub: `5Y: ${fmtPct(m.div_cagr_5y, 1, true)} · 10Y: ${fmtPct(m.div_cagr_10y, 1, true)}`,
    },
    {
      label: "Payout (EPS / FCF)",
      value: `${fmtPct(m.payout_ratio_eps, 0)} / ${fmtPct(m.payout_ratio_fcf, 0)}`,
      sub: "Same fiscal year basis",
    },
    {
      label: hasOpIncome ? "Gross / Op. Margin" : "Gross / Pretax Margin",
      value: `${fmtPct(m.gross_margin, 1)} / ${fmtPct(latestProfitMargin, 1)}`,
      sub: `5Y Rev CAGR: ${fmtPct(m.rev_cagr_5y, 1, true)}`,
    },
    {
      label: "ROIC / ROE",
      value: `${fmtPct(m.roic, 1)} / ${fmtPct(m.roe, 1)}`,
      sub: `Equity Ratio: ${fmtPct(m.equity_ratio, 1)}`,
    },
    {
      label: "5Y Share Count Δ",
      value: fmtPct(m.share_change_5y, 1, true),
      sub: `Adj. FCF Margin (ex-SBC): ${fmtPct(m.fcf_ex_sbc_margin ?? m.fcf_margin, 1)}`,
    },
    {
      label: "Int. Cov. / Curr. Ratio",
      value: `${fmtMultiple(m.interest_coverage)} / ${fmtMultiple(m.current_ratio)}`,
      sub: `Net Debt / FCF: ${fmtMultiple(m.net_debt_to_fcf)}`,
    },
  ];

  root.appendChild(
    el(
      "div",
      { className: "kpi-grid" },
      kpis.map((k) =>
        el("div", { className: "kpi-card" }, [
          el("div", { className: "kpi-label", text: k.label }),
          el("div", { className: "kpi-value", text: k.value }),
          el("div", { className: "kpi-sub", text: k.sub }),
        ])
      )
    )
  );

  // 4. セクションタブ & Visual/Table 切替バー
  const tabsBar = el("div", { className: "section-tabs" });
  const sectionsContainer = el("div");
  root.appendChild(tabsBar);
  root.appendChild(sectionsContainer);

  function renderTabsAndSections() {
    destroyAllCharts();
    clearNode(tabsBar);
    clearNode(sectionsContainer);

    const tabs = [
      { id: "all", label: "All Sections" },
      { id: "pl", label: "Earnings (PL)" },
      { id: "cf", label: "Cash Flow (CF)" },
      { id: "bs", label: "Balance Sheet & Shares" },
      { id: "div", label: "Dividends" },
      { id: "qtr", label: "Quarterly" },
      { id: "etf", label: "ETF Ownership" },
    ];
    const modes = [
      { id: "both", label: "Charts & Tables" },
      { id: "charts", label: "Charts Only" },
      { id: "tables", label: "Tables Only" },
    ];

    tabsBar.appendChild(
      el(
        "div",
        { className: "toolbar-group" },
        tabs.map((t) =>
          el("button", {
            type: "button",
            className: `btn btn-sm${state.activeTab === t.id ? " btn-active" : ""}`,
            text: t.label,
            onClick: () => {
              state.activeTab = t.id;
              renderTabsAndSections();
            },
          })
        )
      )
    );

    tabsBar.appendChild(
      el(
        "div",
        { className: "toolbar-group" },
        modes.map((md) =>
          el("button", {
            type: "button",
            className: `btn btn-sm${state.viewMode === md.id ? " btn-active" : ""}`,
            text: md.label,
            onClick: () => {
              state.viewMode = md.id;
              renderTabsAndSections();
            },
          })
        )
      )
    );

    const showCharts = state.viewMode !== "tables";
    const showTables = state.viewMode !== "charts";
    const compactTables = state.viewMode === "both";
    const fyLabels = annual.map((r) => r.fy_end.slice(0, 4));
    const hasAnnual = annual.length > 0;

    if (!hasAnnual && state.activeTab !== "etf") {
      sectionsContainer.appendChild(
        el("div", {
          className: "notes-box",
          text: "No annual financial statements are available for this security. Historical ETF ownership and implied quarter-end prices are shown below.",
        })
      );
    }

    // =========================================================================
    // 1. Earnings (PL)
    // =========================================================================
    if (hasAnnual && (state.activeTab === "all" || state.activeTab === "pl")) {
      const card = el("section", { className: "card" });
      card.appendChild(
        el("div", { className: "card-header" }, [
          el("h2", { className: "card-title", text: "Earnings (Income Statement)" }),
          el("span", {
            className: "text-muted",
            text: `${annual.length} fiscal years · ${hasOpIncome ? "Operating" : "Pretax"} income basis`,
          }),
        ])
      );
      const body = el("div", { className: "card-body" });

      if (showCharts) {
        const plControls = el("div", { className: "chart-controls" });
        const plCanvasWrap = el("div", { className: "chart-box" });
        let plChart = null;

        const plDefs = [
          {
            key: "revenue",
            label: "Revenue",
            data: annual.map((r) => (r.revenue != null ? +(r.revenue / 1e9).toFixed(2) : null)),
            color: PALETTE.blueSoft,
          },
          {
            key: "gross",
            label: "Gross Profit",
            data: annual.map((r) => (r.gross_profit != null ? +(r.gross_profit / 1e9).toFixed(2) : null)),
            color: PALETTE.indigoSoft,
          },
          {
            key: "profit",
            label: profitLabel,
            data: annual.map((r) => {
              const v = getProfit(r);
              return v != null ? +(v / 1e9).toFixed(2) : null;
            }),
            color: PALETTE.emeraldSoft,
          },
          {
            key: "net",
            label: "Net Income",
            data: annual.map((r) => (r.net_income != null ? +(r.net_income / 1e9).toFixed(2) : null)),
            color: PALETTE.amberSoft,
          },
          {
            key: "rd",
            label: "R&D",
            data: annual.map((r) => (r.rd_expense != null ? +(r.rd_expense / 1e9).toFixed(2) : null)),
            color: PALETTE.violetSoft,
          },
          {
            key: "sga",
            label: "SG&A",
            data: annual.map((r) => (r.sga_expense != null ? +(r.sga_expense / 1e9).toFixed(2) : null)),
            color: PALETTE.slateSoft,
          },
        ].filter((d) => hasAnyValue(d.data));

        function renderPlChart() {
          destroyChart(plChart);
          clearNode(plControls);
          clearNode(plCanvasWrap);

          for (const def of plDefs) {
            const active = Boolean(state.plSeries[def.key]);
            plControls.appendChild(
              el("button", {
                type: "button",
                className: `chip-toggle${active ? " active" : ""}`,
                text: `${active ? "✓ " : "+ "}${def.label}`,
                onClick: () => {
                  const activeCount = plDefs.filter((d) => state.plSeries[d.key]).length;
                  if (active && activeCount <= 1) return;
                  state.plSeries[def.key] = !active;
                  renderPlChart();
                },
              })
            );
          }

          const canvas = el("canvas", { ariaLabel: "Revenue and earnings chart" });
          plCanvasWrap.appendChild(canvas);
          const datasets = plDefs
            .filter((d) => state.plSeries[d.key])
            .map((d) => ({ label: d.label, data: d.data, backgroundColor: d.color }));

          plChart = createChart(canvas, {
            type: "bar",
            data: { labels: fyLabels, datasets },
            options: baseChartOptions(fmtAxisUsdB),
          });
        }

        // 右グラフ: Diluted EPS + Op/Pretax Margin + Gross Margin（有効な系列のみ表示）
        const c2 = el("canvas", { ariaLabel: "EPS and profit margins chart" });
        const epsData = annual.map((r) => r.eps_diluted);
        const profMarginData = annual.map((r) => {
          const v = getProfit(r);
          return v != null && r.revenue > 0 ? +((v / r.revenue) * 100).toFixed(1) : null;
        });
        const grossMarginData = annual.map((r) =>
          r.gross_profit != null && r.revenue > 0 ? +((r.gross_profit / r.revenue) * 100).toFixed(1) : null
        );

        const rightDatasets = [];
        if (hasAnyValue(epsData)) {
          rightDatasets.push({
            label: "Diluted EPS ($)",
            data: epsData,
            backgroundColor: PALETTE.blueSoft,
            yAxisID: "y",
            order: 3,
          });
        }
        let hasRightAxis = false;
        if (hasAnyValue(profMarginData)) {
          hasRightAxis = true;
          rightDatasets.push({
            type: "line",
            label: profitMarginLabel,
            data: profMarginData,
            borderColor: PALETTE.emerald,
            backgroundColor: PALETTE.emerald,
            borderWidth: 2.2,
            tension: 0.2,
            yAxisID: "y1",
            order: 1,
          });
        }
        // Gross Margin が最新 3 年度以内に存在する場合のみ右グラフに補助表示（過去途中で途切れた企業の見栄え崩れ防止）
        const hasRecentGrossMargin = hasAnyValue(grossMarginData.slice(-3));
        if (hasRecentGrossMargin) {
          hasRightAxis = true;
          rightDatasets.push({
            type: "line",
            label: "Gross Margin (%)",
            data: grossMarginData,
            borderColor: PALETTE.amber,
            backgroundColor: PALETTE.amber,
            borderWidth: 1.8,
            borderDash: [4, 3],
            tension: 0.2,
            yAxisID: "y1",
            order: 2,
          });
        }

        body.appendChild(
          el("div", { className: "two-col-charts" }, [
            el("div", {}, [
              el("h3", { className: "sub-chart-title", text: "Revenue · Profit · Cost Structure ($B)" }),
              plControls,
              plCanvasWrap,
            ]),
            el("div", {}, [
              el("h3", {
                className: "sub-chart-title",
                text: `Diluted EPS ($) & ${hasOpIncome ? "Margins" : "Pretax / Gross Margins"} (%)`,
              }),
              el("div", { className: "chart-box" }, [c2]),
            ]),
          ])
        );

        renderPlChart();
        createChart(c2, {
          type: "bar",
          data: { labels: fyLabels, datasets: rightDatasets },
          options: baseChartOptions(fmtAxisUsd, hasRightAxis ? fmtAxisPct : null),
        });
      }

      if (showTables) {
        body.appendChild(
          buildTable(
            [
              { label: "FY End", num: false },
              { label: "Revenue", num: true },
              { label: "Gross Profit", num: true },
              { label: "Gross Mgn", num: true },
              { label: profitLabel, num: true },
              { label: hasOpIncome ? "Op. Mgn" : "Pretax Mgn", num: true },
              { label: "Net Income", num: true },
              { label: "R&D", num: true },
              { label: "Diluted EPS", num: true },
            ],
            annualDesc.map((r) => {
              const pv = getProfit(r);
              return [
                r.fy_end,
                fmtUsdCompact(r.revenue),
                fmtUsdCompact(r.gross_profit),
                r.gross_profit != null && r.revenue > 0 ? fmtPct(r.gross_profit / r.revenue, 1) : "—",
                fmtUsdCompact(pv),
                pv != null && r.revenue > 0 ? fmtPct(pv / r.revenue, 1) : "—",
                fmtUsdCompact(r.net_income),
                fmtUsdCompact(r.rd_expense),
                fmtPerShare(r.eps_diluted),
              ];
            }),
            compactTables
          )
        );
      }

      // PL 項目の欠損・代替理由を自動注記
      const plNotes = [];
      if (!hasOpIncome) {
        plNotes.push(
          "Operating Income is not separately reported in standard US-GAAP for this issuer; Pretax Income is shown instead."
        );
      }
      const gpRows = annual.filter((r) => r.gross_profit != null);
      if (gpRows.length === 0) {
        plNotes.push("Gross Profit is not reported in standard US-GAAP tags for this issuer.");
      } else if (
        latestAnn &&
        gpRows[gpRows.length - 1].fy_end < latestAnn.fy_end &&
        !hasAnyValue(annual.slice(-3).map((r) => r.gross_profit))
      ) {
        const lastGpYr = gpRows[gpRows.length - 1].fy_end.slice(0, 4);
        plNotes.push(
          `Gross Profit is only available through FY${lastGpYr} in standard US-GAAP (issuer switched to custom XBRL extension tags thereafter).`
        );
      }
      if (plNotes.length > 0) {
        body.appendChild(el("p", { className: "section-note", text: plNotes.join(" ") }));
      }

      card.appendChild(body);
      sectionsContainer.appendChild(card);
    }

    // =========================================================================
    // 2. Cash Flow & Shareholder Return
    // =========================================================================
    if (hasAnnual && (state.activeTab === "all" || state.activeTab === "cf")) {
      const card = el("section", { className: "card" });
      card.appendChild(
        el("div", { className: "card-header" }, [
          el("h2", { className: "card-title", text: "Cash Flow & Shareholder Return" }),
          el("span", {
            className: "text-muted",
            text: "Toggle series to adjust scale · Adj. FCF = FCF − Stock-Based Comp",
          }),
        ])
      );
      const body = el("div", { className: "card-body" });

      if (showCharts) {
        const cfControls = el("div", { className: "chart-controls" });
        const cfCanvasWrap = el("div", { className: "chart-box" });
        let cfChart = null;

        const retControls = el("div", { className: "chart-controls" });
        const retCanvasWrap = el("div", { className: "chart-box" });
        let retChart = null;

        const cfDefs = [
          {
            key: "ocf",
            label: "Operating CF",
            data: annual.map((r) => (r.operating_cf != null ? +(r.operating_cf / 1e9).toFixed(2) : null)),
            build: (data) => ({ label: "Operating CF", data, backgroundColor: PALETTE.blueSoft, order: 2 }),
          },
          {
            key: "capex",
            label: "CapEx",
            data: annual.map((r) => (r.capex != null ? +(-r.capex / 1e9).toFixed(2) : null)),
            build: (data) => ({ label: "CapEx", data, backgroundColor: PALETTE.roseSoft, order: 2 }),
          },
          {
            key: "fcf",
            label: "Free Cash Flow",
            data: annual.map((r) => (r.fcf != null ? +(r.fcf / 1e9).toFixed(2) : null)),
            build: (data) => ({
              type: "line",
              label: "Free Cash Flow",
              data,
              borderColor: PALETTE.emerald,
              backgroundColor: PALETTE.emerald,
              borderWidth: 2.5,
              tension: 0.2,
              order: 1,
            }),
          },
          {
            key: "adjFcf",
            label: "Adj. FCF (ex-SBC)",
            data: annual.map((r) =>
              r.fcf != null && r.sbc != null ? +((r.fcf - r.sbc) / 1e9).toFixed(2) : null
            ),
            build: (data) => ({
              type: "line",
              label: "Adj. FCF (ex-SBC)",
              data,
              borderColor: PALETTE.indigo,
              backgroundColor: PALETTE.indigo,
              borderWidth: 2,
              borderDash: [4, 3],
              tension: 0.2,
              order: 1,
            }),
          },
          {
            key: "icf",
            label: "Investing CF",
            data: annual.map((r) => (r.investing_cf != null ? +(r.investing_cf / 1e9).toFixed(2) : null)),
            build: (data) => ({ label: "Investing CF", data, backgroundColor: PALETTE.amberSoft, order: 2 }),
          },
          {
            key: "fin",
            label: "Financing CF",
            data: annual.map((r) => (r.financing_cf != null ? +(r.financing_cf / 1e9).toFixed(2) : null)),
            build: (data) => ({ label: "Financing CF", data, backgroundColor: PALETTE.violetSoft, order: 2 }),
          },
        ].filter((d) => hasAnyValue(d.data));

        function renderCfChart() {
          destroyChart(cfChart);
          clearNode(cfControls);
          clearNode(cfCanvasWrap);

          for (const def of cfDefs) {
            const active = Boolean(state.cfSeries[def.key]);
            cfControls.appendChild(
              el("button", {
                type: "button",
                className: `chip-toggle${active ? " active" : ""}`,
                text: `${active ? "✓ " : "+ "}${def.label}`,
                onClick: () => {
                  const activeCount = cfDefs.filter((d) => state.cfSeries[d.key]).length;
                  if (active && activeCount <= 1) return;
                  state.cfSeries[def.key] = !active;
                  renderCfChart();
                },
              })
            );
          }

          const canvas = el("canvas", { ariaLabel: "Cash flow statement chart" });
          cfCanvasWrap.appendChild(canvas);
          const datasets = cfDefs.filter((d) => state.cfSeries[d.key]).map((d) => d.build(d.data));
          cfChart = createChart(canvas, {
            type: "bar",
            data: { labels: fyLabels, datasets },
            options: baseChartOptions(fmtAxisUsdB),
          });
        }

        const returnModes = [
          { id: "stacked", label: "Stacked (Total)" },
          { id: "total", label: "Total Only" },
          { id: "dividends", label: "Dividends Only" },
          { id: "buybacks", label: "Buybacks Only" },
        ];

        function renderReturnChart() {
          destroyChart(retChart);
          clearNode(retControls);
          clearNode(retCanvasWrap);

          for (const rm of returnModes) {
            retControls.appendChild(
              el("button", {
                type: "button",
                className: `chip-toggle${state.returnMode === rm.id ? " active" : ""}`,
                text: rm.label,
                onClick: () => {
                  state.returnMode = rm.id;
                  renderReturnChart();
                },
              })
            );
          }

          const divData = annual.map((r) =>
            r.dividends_paid != null ? +(r.dividends_paid / 1e9).toFixed(2) : null
          );
          const buyData = annual.map((r) =>
            r.buybacks != null ? +(r.buybacks / 1e9).toFixed(2) : null
          );
          const totalData = annual.map((r) => {
            if (r.dividends_paid == null && r.buybacks == null) return null;
            return +(((r.dividends_paid || 0) + (r.buybacks || 0)) / 1e9).toFixed(2);
          });

          const canvas = el("canvas", { ariaLabel: "Shareholder return chart" });
          retCanvasWrap.appendChild(canvas);

          let datasets = [];
          let extraOpts = {};

          if (state.returnMode === "stacked") {
            datasets = [
              { label: "Dividends Paid", data: divData, backgroundColor: PALETTE.emeraldSoft },
              { label: "Share Buybacks", data: buyData, backgroundColor: PALETTE.indigoSoft },
            ];
            extraOpts = {
              stacked: true,
              tooltip: {
                callbacks: {
                  footer: (items) => {
                    const sum = items.reduce((acc, it) => acc + (Number(it.parsed?.y) || 0), 0);
                    return `Total Return: $${fmtAxisNum(sum, 2)}B`;
                  },
                },
              },
            };
          } else if (state.returnMode === "total") {
            datasets = [
              { label: "Total Return (Div + Buybacks)", data: totalData, backgroundColor: PALETTE.blueSoft },
            ];
          } else if (state.returnMode === "dividends") {
            datasets = [{ label: "Dividends Paid", data: divData, backgroundColor: PALETTE.emeraldSoft }];
          } else {
            datasets = [{ label: "Share Buybacks", data: buyData, backgroundColor: PALETTE.indigoSoft }];
          }

          retChart = createChart(canvas, {
            type: "bar",
            data: { labels: fyLabels, datasets },
            options: baseChartOptions(fmtAxisUsdB, null, extraOpts),
          });
        }

        body.appendChild(
          el("div", { className: "two-col-charts" }, [
            el("div", {}, [
              el("h3", { className: "sub-chart-title", text: "Cash Flows ($B)" }),
              cfControls,
              cfCanvasWrap,
            ]),
            el("div", {}, [
              el("h3", { className: "sub-chart-title", text: "Shareholder Return ($B)" }),
              retControls,
              retCanvasWrap,
            ]),
          ])
        );

        renderCfChart();
        renderReturnChart();
      }

      if (showTables) {
        body.appendChild(
          buildTable(
            [
              { label: "FY End", num: false },
              { label: "Operating CF", num: true },
              { label: "Investing CF", num: true },
              { label: "Financing CF", num: true },
              { label: "CapEx", num: true },
              { label: "FCF", num: true },
              { label: "SBC", num: true },
              { label: "Adj. FCF", num: true },
              { label: "Dividends", num: true },
              { label: "Buybacks", num: true },
              { label: "Total Return", num: true },
            ],
            annualDesc.map((r) => {
              const adjFcf = r.fcf != null && r.sbc != null ? r.fcf - r.sbc : null;
              const totalRet =
                r.dividends_paid != null || r.buybacks != null
                  ? (r.dividends_paid || 0) + (r.buybacks || 0)
                  : null;
              return [
                r.fy_end,
                fmtUsdCompact(r.operating_cf),
                fmtUsdCompact(r.investing_cf),
                fmtUsdCompact(r.financing_cf),
                fmtUsdCompact(r.capex),
                fmtUsdCompact(r.fcf),
                fmtUsdCompact(r.sbc),
                fmtUsdCompact(adjFcf),
                fmtUsdCompact(r.dividends_paid),
                fmtUsdCompact(r.buybacks),
                fmtUsdCompact(totalRet),
              ];
            }),
            compactTables
          )
        );
      }

      if (!hasAnyValue(annual.slice(-3).map((r) => r.capex))) {
        body.appendChild(
          el("p", {
            className: "section-note",
            text: "CapEx is not separately reported in standard SEC XBRL cash flow tags for recent fiscal years; Free Cash Flow is shown as — where unavailable.",
          })
        );
      }

      card.appendChild(body);
      sectionsContainer.appendChild(card);
    }

    // =========================================================================
    // 3. Balance Sheet, Share Count & Capital Efficiency
    // =========================================================================
    if (hasAnnual && (state.activeTab === "all" || state.activeTab === "bs")) {
      const card = el("section", { className: "card" });
      card.appendChild(
        el("div", { className: "card-header" }, [
          el("h2", { className: "card-title", text: "Balance Sheet, Share Count & Financial Health" }),
          el("span", {
            className: "text-muted",
            text: "Split-adjusted share count · Net Debt · ROIC · Liquidity",
          }),
        ])
      );
      const body = el("div", { className: "card-body" });

      if (showCharts) {
        const bsControls = el("div", { className: "chart-controls" });
        const bsCanvasWrap = el("div", { className: "chart-box" });
        let bsChart = null;

        const rightControls = el("div", { className: "chart-controls" });
        const rightCanvasWrap = el("div", { className: "chart-box" });
        let bsRightChart = null;

        const bsDefs = [
          {
            key: "assets",
            label: "Assets",
            isRight: false,
            data: annual.map((r) => (r.total_assets != null ? +(r.total_assets / 1e9).toFixed(2) : null)),
            build: (data) => ({ label: "Total Assets", data, backgroundColor: PALETTE.slateSoft, yAxisID: "y", order: 2 }),
          },
          {
            key: "equity",
            label: "Equity",
            isRight: false,
            data: annual.map((r) => (r.total_equity != null ? +(r.total_equity / 1e9).toFixed(2) : null)),
            build: (data) => ({ label: "Total Equity", data, backgroundColor: PALETTE.blueSoft, yAxisID: "y", order: 2 }),
          },
          {
            key: "debt",
            label: "Total Debt",
            isRight: false,
            data: annual.map((r) => (r.total_debt != null ? +(r.total_debt / 1e9).toFixed(2) : null)),
            build: (data) => ({ label: "Total Debt", data, backgroundColor: PALETTE.roseSoft, yAxisID: "y", order: 2 }),
          },
          {
            key: "cash",
            label: "Cash",
            isRight: false,
            data: annual.map((r) => (r.cash != null ? +(r.cash / 1e9).toFixed(2) : null)),
            build: (data) => ({ label: "Cash", data, backgroundColor: PALETTE.emeraldSoft, yAxisID: "y", order: 2 }),
          },
          {
            key: "netDebt",
            label: "Net Debt",
            isRight: false,
            data: annual.map((r) =>
              r.total_debt != null ? +((r.total_debt - (r.cash || 0)) / 1e9).toFixed(2) : null
            ),
            build: (data) => ({
              type: "line",
              label: "Net Debt",
              data,
              borderColor: PALETTE.rose,
              backgroundColor: PALETTE.rose,
              borderWidth: 2,
              tension: 0.2,
              yAxisID: "y",
              order: 1,
            }),
          },
          {
            key: "goodwill",
            label: "Goodwill & Intang.",
            isRight: false,
            data: annual.map((r) =>
              r.goodwill != null || r.intangibles != null
                ? +(((r.goodwill || 0) + (r.intangibles || 0)) / 1e9).toFixed(2)
                : null
            ),
            build: (data) => ({
              label: "Goodwill & Intang.",
              data,
              backgroundColor: PALETTE.violetSoft,
              yAxisID: "y",
              order: 2,
            }),
          },
          {
            key: "eqRatio",
            label: "Equity Ratio (%)",
            isRight: true,
            data: annual.map((r) =>
              r.total_equity != null && r.total_assets > 0
                ? +((r.total_equity / r.total_assets) * 100).toFixed(1)
                : null
            ),
            build: (data) => ({
              type: "line",
              label: "Equity Ratio (%)",
              data,
              borderColor: PALETTE.amber,
              backgroundColor: PALETTE.amber,
              borderWidth: 2,
              tension: 0.2,
              yAxisID: "y1",
              order: 1,
            }),
          },
        ].filter((d) => hasAnyValue(d.data));

        function renderBsChart() {
          destroyChart(bsChart);
          clearNode(bsControls);
          clearNode(bsCanvasWrap);

          for (const def of bsDefs) {
            const active = Boolean(state.bsSeries[def.key]);
            bsControls.appendChild(
              el("button", {
                type: "button",
                className: `chip-toggle${active ? " active" : ""}`,
                text: `${active ? "✓ " : "+ "}${def.label}`,
                onClick: () => {
                  const activeCount = bsDefs.filter((d) => state.bsSeries[d.key]).length;
                  if (active && activeCount <= 1) return;
                  state.bsSeries[def.key] = !active;
                  renderBsChart();
                },
              })
            );
          }

          const selected = bsDefs.filter((d) => state.bsSeries[d.key]);
          const useRightAxis = selected.some((d) => d.isRight);
          const canvas = el("canvas", { ariaLabel: "Balance sheet chart" });
          bsCanvasWrap.appendChild(canvas);

          bsChart = createChart(canvas, {
            type: "bar",
            data: { labels: fyLabels, datasets: selected.map((d) => d.build(d.data)) },
            options: baseChartOptions(fmtAxisUsdB, useRightAxis ? fmtAxisPct : null),
          });
        }

        function renderBsRightChart() {
          destroyChart(bsRightChart);
          clearNode(rightControls);
          clearNode(rightCanvasWrap);

          for (const opt of [
            { id: "shares", label: "Share Count (Buyback Effect)" },
            { id: "health", label: "ROIC & Liquidity" },
          ]) {
            rightControls.appendChild(
              el("button", {
                type: "button",
                className: `chip-toggle${state.bsRightMode === opt.id ? " active" : ""}`,
                text: opt.label,
                onClick: () => {
                  state.bsRightMode = opt.id;
                  renderBsRightChart();
                },
              })
            );
          }

          const canvas = el("canvas", { ariaLabel: "Share count and financial health chart" });
          rightCanvasWrap.appendChild(canvas);

          if (state.bsRightMode === "shares") {
            const shareM = annual.map((r) => {
              const sh = r.shares_diluted || r.shares_outstanding;
              return sh != null ? +(sh / 1e6).toFixed(1) : null;
            });
            bsRightChart = createChart(canvas, {
              type: "bar",
              data: {
                labels: fyLabels,
                datasets: [
                  {
                    label: "Diluted Shares (M, Split-Adj.)",
                    data: shareM,
                    backgroundColor: PALETTE.indigoSoft,
                  },
                ],
              },
              options: baseChartOptions((v) => `${fmtAxisNum(v, 0)}M`),
            });
          } else {
            const roicData = annual.map((r) => {
              const v = calcAnnualRoic(r);
              return v != null && v > -1 && v < 2 ? +(v * 100).toFixed(1) : null;
            });
            const currData = annual.map((r) =>
              r.current_assets != null && r.current_liabilities > 0
                ? +(r.current_assets / r.current_liabilities).toFixed(2)
                : null
            );
            const ds = [];
            if (hasAnyValue(roicData)) {
              ds.push({
                type: "line",
                label: "ROIC (%)",
                data: roicData,
                borderColor: PALETTE.blue,
                backgroundColor: PALETTE.blue,
                borderWidth: 2.2,
                tension: 0.2,
                yAxisID: "y",
                order: 1,
              });
            }
            const hasCurr = hasAnyValue(currData);
            if (hasCurr) {
              ds.push({
                type: "bar",
                label: "Current Ratio (x)",
                data: currData,
                backgroundColor: PALETTE.emeraldSoft,
                yAxisID: hasAnyValue(roicData) ? "y1" : "y",
                order: 2,
              });
            }
            const leftFmt = hasAnyValue(roicData) ? fmtAxisPct : (v) => `${fmtAxisNum(v, 1)}x`;
            const rightFmt = hasAnyValue(roicData) && hasCurr ? (v) => `${fmtAxisNum(v, 1)}x` : null;
            bsRightChart = createChart(canvas, {
              type: "bar",
              data: { labels: fyLabels, datasets: ds },
              options: baseChartOptions(leftFmt, rightFmt),
            });
          }
        }

        body.appendChild(
          el("div", { className: "two-col-charts" }, [
            el("div", {}, [
              el("h3", { className: "sub-chart-title", text: "Balance Sheet Structure ($B)" }),
              bsControls,
              bsCanvasWrap,
            ]),
            el("div", {}, [
              el("h3", { className: "sub-chart-title", text: "Share Count Trend & Capital Efficiency" }),
              rightControls,
              rightCanvasWrap,
            ]),
          ])
        );

        renderBsChart();
        renderBsRightChart();
      }

      if (showTables) {
        body.appendChild(
          buildTable(
            [
              { label: "FY End", num: false },
              { label: "Assets", num: true },
              { label: "Equity", num: true },
              { label: "Cash", num: true },
              { label: "Total Debt", num: true },
              { label: "Net Debt", num: true },
              { label: "Goodwill", num: true },
              { label: "Curr. Ratio", num: true },
              { label: "Int. Cov.", num: true },
              { label: "ROIC", num: true },
              { label: "Shares", num: true },
            ],
            annualDesc.map((r) => {
              const netDebt = r.total_debt != null ? r.total_debt - (r.cash || 0) : null;
              const currRatio =
                r.current_assets != null && r.current_liabilities > 0
                  ? r.current_assets / r.current_liabilities
                  : null;
              return [
                r.fy_end,
                fmtUsdCompact(r.total_assets),
                fmtUsdCompact(r.total_equity),
                fmtUsdCompact(r.cash),
                fmtUsdCompact(r.total_debt),
                fmtUsdCompact(netDebt),
                fmtUsdCompact(r.goodwill),
                fmtMultiple(currRatio),
                fmtMultiple(calcInterestCoverage(r)),
                fmtPct(calcAnnualRoic(r), 1),
                fmtSharesM(r.shares_diluted || r.shares_outstanding),
              ];
            }),
            compactTables
          )
        );
      }

      const bsNotes = [];
      if (!hasAnyValue(annual.map((r) => r.total_debt))) {
        bsNotes.push("No short-term or long-term debt reported in standard SEC XBRL balance sheet tags.");
      }
      if (!hasAnyValue(annual.map((r) => r.current_assets))) {
        bsNotes.push(
          "Unclassified balance sheet (common for banks and insurers): Current Ratio is not applicable."
        );
      }
      if (bsNotes.length > 0) {
        body.appendChild(el("p", { className: "section-note", text: bsNotes.join(" ") }));
      }

      card.appendChild(body);
      sectionsContainer.appendChild(card);
    }

    // =========================================================================
    // 4. Dividends
    // =========================================================================
    if (hasAnnual && (state.activeTab === "all" || state.activeTab === "div")) {
      const card = el("section", { className: "card" });
      card.appendChild(
        el("div", { className: "card-header" }, [
          el("h2", { className: "card-title", text: "Dividends & Payout Sustainability" }),
          el("span", { className: "text-muted", text: "Split-adjusted DPS & same-period payout ratios" }),
        ])
      );
      const body = el("div", { className: "card-body" });

      if (showCharts) {
        const c1 = el("canvas", { ariaLabel: "Dividends per share and payout ratio chart" });
        body.appendChild(el("div", { className: "chart-box-tall" }, [c1]));

        const dpsData = annual.map((r) => {
          const d = effectiveDps(r);
          return d != null ? +d.toFixed(3) : null;
        });
        const payEpsData = annual.map((r) => {
          const d = effectiveDps(r);
          if (d != null && r.eps_diluted > 0) {
            const pct = (d / r.eps_diluted) * 100;
            return pct <= 250 ? +pct.toFixed(1) : null;
          }
          return null;
        });
        const payFcfData = annual.map((r) => {
          if (r.dividends_paid != null && r.fcf > 0) {
            const pct = (r.dividends_paid / r.fcf) * 100;
            return pct <= 250 ? +pct.toFixed(1) : null;
          }
          return null;
        });

        const divDs = [];
        if (hasAnyValue(dpsData)) {
          divDs.push({
            label: "DPS (Split-Adjusted $)",
            data: dpsData,
            backgroundColor: PALETTE.emeraldSoft,
            yAxisID: "y",
            order: 2,
          });
        }
        let hasRight = false;
        if (hasAnyValue(payEpsData)) {
          hasRight = true;
          divDs.push({
            type: "line",
            label: "Payout vs EPS (%)",
            data: payEpsData,
            borderColor: PALETTE.blue,
            backgroundColor: PALETTE.blue,
            borderWidth: 2,
            tension: 0.2,
            yAxisID: "y1",
            order: 1,
          });
        }
        if (hasAnyValue(payFcfData)) {
          hasRight = true;
          divDs.push({
            type: "line",
            label: "Payout vs FCF (%)",
            data: payFcfData,
            borderColor: PALETTE.amber,
            backgroundColor: PALETTE.amber,
            borderWidth: 2,
            borderDash: [4, 3],
            tension: 0.2,
            yAxisID: "y1",
            order: 1,
          });
        }

        createChart(c1, {
          type: "bar",
          data: { labels: fyLabels, datasets: divDs },
          options: baseChartOptions(fmtAxisUsd, hasRight ? fmtAxisPct : null),
        });
      }

      if (showTables) {
        body.appendChild(
          buildTable(
            [
              { label: "FY End", num: false },
              { label: "DPS Declared", num: true },
              { label: "DPS Paid", num: true },
              { label: "Effective DPS", num: true },
              { label: "Dividends Paid", num: true },
              { label: "Payout (EPS)", num: true },
              { label: "Payout (FCF)", num: true },
            ],
            annualDesc.map((r) => {
              const eff = effectiveDps(r);
              return [
                r.fy_end,
                fmtPerShare(r.dps_declared),
                fmtPerShare(r.dps_paid),
                fmtPerShare(eff),
                fmtUsdCompact(r.dividends_paid),
                eff != null && r.eps_diluted > 0 ? fmtPct(eff / r.eps_diluted, 1) : "—",
                r.dividends_paid != null && r.fcf > 0 ? fmtPct(r.dividends_paid / r.fcf, 1) : "—",
              ];
            }),
            compactTables
          )
        );
      }

      card.appendChild(body);
      sectionsContainer.appendChild(card);
    }

    // =========================================================================
    // 5. Quarterly Trends (10-Q + Q4 derived)
    // =========================================================================
    if (quarterly.length > 0 && (state.activeTab === "all" || state.activeTab === "qtr")) {
      const card = el("section", { className: "card" });
      card.appendChild(
        el("div", { className: "card-header" }, [
          el("h2", { className: "card-title", text: "Quarterly Performance" }),
          el("span", {
            className: "text-muted",
            text: `Recent ${Math.min(quarterly.length, 12)} quarters · Standalone 3-month figures & YoY growth`,
          }),
        ])
      );
      const body = el("div", { className: "card-body" });

      // 前年同期比（4 四半期前との比較）を算出してから直近 12 四半期を切り出す
      const qWithYoy = quarterly.map((q, idx) => {
        const prev = idx >= 4 ? quarterly[idx - 4] : null;
        const revYoy =
          prev && prev.revenue > 0 && q.revenue != null ? (q.revenue - prev.revenue) / prev.revenue : null;
        return { ...q, revYoy };
      });
      const recentQ = qWithYoy.slice(-12);
      const qLabels = recentQ.map((q) => q.period_end.slice(0, 7));

      if (showCharts) {
        const c1 = el("canvas", { ariaLabel: "Quarterly revenue and YoY growth chart" });
        const c2 = el("canvas", { ariaLabel: "Quarterly EPS and FCF chart" });
        body.appendChild(
          el("div", { className: "two-col-charts" }, [
            el("div", {}, [
              el("h3", { className: "sub-chart-title", text: "Quarterly Revenue · Net Income ($B) & Rev YoY (%)" }),
              el("div", { className: "chart-box" }, [c1]),
            ]),
            el("div", {}, [
              el("h3", { className: "sub-chart-title", text: "Quarterly Diluted EPS ($) & Free Cash Flow ($B)" }),
              el("div", { className: "chart-box" }, [c2]),
            ]),
          ])
        );

        const qRevYoyData = recentQ.map((q) => (q.revYoy != null ? +(q.revYoy * 100).toFixed(1) : null));
        const hasYoy = hasAnyValue(qRevYoyData);
        const qDs1 = [
          {
            label: "Revenue ($B)",
            data: recentQ.map((q) => (q.revenue != null ? +(q.revenue / 1e9).toFixed(2) : null)),
            backgroundColor: PALETTE.blueSoft,
            yAxisID: "y",
            order: 2,
          },
          {
            label: "Net Income ($B)",
            data: recentQ.map((q) => (q.net_income != null ? +(q.net_income / 1e9).toFixed(2) : null)),
            backgroundColor: PALETTE.amberSoft,
            yAxisID: "y",
            order: 2,
          },
        ];
        if (hasYoy) {
          qDs1.push({
            type: "line",
            label: "Revenue YoY (%)",
            data: qRevYoyData,
            borderColor: PALETTE.emerald,
            backgroundColor: PALETTE.emerald,
            borderWidth: 2,
            tension: 0.2,
            yAxisID: "y1",
            order: 1,
          });
        }
        createChart(c1, {
          type: "bar",
          data: { labels: qLabels, datasets: qDs1 },
          options: baseChartOptions(fmtAxisUsdB, hasYoy ? fmtAxisPct : null),
        });

        const qEpsData = recentQ.map((q) => q.eps_diluted);
        const qFcfData = recentQ.map((q) => (q.fcf != null ? +(q.fcf / 1e9).toFixed(2) : null));
        const hasQFcf = hasAnyValue(qFcfData);
        const qDs2 = [];
        if (hasAnyValue(qEpsData)) {
          qDs2.push({
            label: "Diluted EPS ($)",
            data: qEpsData,
            backgroundColor: PALETTE.indigoSoft,
            yAxisID: "y",
            order: 2,
          });
        }
        if (hasQFcf) {
          qDs2.push({
            type: "line",
            label: "Free Cash Flow ($B)",
            data: qFcfData,
            borderColor: PALETTE.emerald,
            backgroundColor: PALETTE.emerald,
            borderWidth: 2,
            tension: 0.2,
            yAxisID: hasAnyValue(qEpsData) ? "y1" : "y",
            order: 1,
          });
        }
        createChart(c2, {
          type: "bar",
          data: { labels: qLabels, datasets: qDs2 },
          options: baseChartOptions(
            hasAnyValue(qEpsData) ? fmtAxisUsd : fmtAxisUsdB,
            hasAnyValue(qEpsData) && hasQFcf ? fmtAxisUsdB : null
          ),
        });
      }

      if (showTables) {
        body.appendChild(
          buildTable(
            [
              { label: "Quarter End", num: false },
              { label: "Revenue", num: true },
              { label: "Rev YoY", num: true },
              { label: profitLabel, num: true },
              { label: "Net Income", num: true },
              { label: "Diluted EPS", num: true },
              { label: "Operating CF", num: true },
              { label: "FCF", num: true },
            ],
            [...recentQ].reverse().map((q) => [
              q.period_end,
              fmtUsdCompact(q.revenue),
              fmtPct(q.revYoy, 1, true),
              fmtUsdCompact(hasOpIncome ? q.operating_income : q.pretax_income),
              fmtUsdCompact(q.net_income),
              fmtPerShare(q.eps_diluted),
              fmtUsdCompact(q.operating_cf),
              fmtUsdCompact(q.fcf),
            ]),
            compactTables
          )
        );
      }

      card.appendChild(body);
      sectionsContainer.appendChild(card);
    }

    // =========================================================================
    // 6. ETF Ownership History
    // =========================================================================
    if (state.activeTab === "all" || state.activeTab === "etf") {
      const card = el("section", { className: "card" });
      card.appendChild(
        el("div", { className: "card-header" }, [
          el("h2", { className: "card-title", text: "ETF Ownership History & Quarter-End Price" }),
          el("span", {
            className: "text-muted",
            text: `Observed across ${detail.prices?.length || 0} quarter-end reports`,
          }),
        ])
      );
      const body = el("div", { className: "card-body" });

      const histSeries = detail.etf_history?.[primaryEtf] || [];
      if (showCharts && histSeries.length > 0) {
        const c1 = el("canvas", { ariaLabel: "ETF weight and quarter-end price chart" });
        body.appendChild(el("div", { className: "chart-box" }, [c1]));

        const priceMap = new Map((detail.prices || []).map((p) => [p.as_of, p.price_usd]));
        createChart(c1, {
          type: "line",
          data: {
            labels: histSeries.map((x) => x.as_of.slice(0, 7)),
            datasets: [
              {
                label: `${primaryEtf} Weight (%)`,
                data: histSeries.map((x) => +(x.weight * 100).toFixed(2)),
                borderColor: PALETTE.blue,
                backgroundColor: "rgba(37, 99, 235, 0.12)",
                fill: true,
                borderWidth: 2,
                tension: 0.15,
                yAxisID: "y",
              },
              {
                label: "Implied Price ($)",
                data: histSeries.map((x) => priceMap.get(x.as_of) ?? null),
                borderColor: PALETTE.emerald,
                backgroundColor: PALETTE.emerald,
                borderWidth: 2,
                tension: 0.15,
                yAxisID: "y1",
              },
            ],
          },
          options: baseChartOptions(fmtAxisPct, fmtAxisUsd),
        });
      }

      if (showTables && histSeries.length > 0) {
        body.appendChild(
          buildTable(
            [
              { label: "Quarter End", num: false },
              { label: "ETF", num: false },
              { label: "Rank", num: true },
              { label: "Weight", num: true },
              { label: "Shares Held", num: true },
              { label: "Market Value", num: true },
              { label: "Implied Price", num: true },
            ],
            [...histSeries].reverse().map((x) => [
              x.as_of,
              primaryEtf,
              `#${x.rank}`,
              fmtPct(x.weight, 2),
              fmtInt(x.shares),
              fmtUsdCompact(x.value_usd),
              x.shares > 0 ? fmtPrice(x.value_usd / x.shares) : "—",
            ]),
            compactTables
          )
        );
      }

      card.appendChild(body);
      sectionsContainer.appendChild(card);
    }

    const metaFootItems = [
      "Standardized SEC XBRL data generally begins around FY2009–2011 (SEC XBRL mandate).",
      detail.cik ? `CIK ${detail.cik}` : null,
      detail.cusip ? `CUSIP ${detail.cusip}` : null,
      detail.isin ? `ISIN ${detail.isin}` : null,
      m.data_notes ? `Notes: ${m.data_notes}` : null,
    ].filter(Boolean);
    sectionsContainer.appendChild(
      el("div", { className: "notes-box", text: metaFootItems.join(" · ") })
    );
  }

  renderTabsAndSections();
}
