/**
 * Chart.js rendering wrapper and axis formatters.
 * - Centrally tracks and destroys active Chart instances on view switches to avoid memory leaks.
 * - Rounds and formats all axis ticks cleanly to prevent floating-point artifacts (e.g. 4.800000000000001%).
 */

const activeCharts = new Set();

export function destroyAllCharts() {
  for (const chart of activeCharts) {
    try {
      chart.destroy();
    } catch {
      // Ignore if already destroyed
    }
  }
  activeCharts.clear();
}

export function destroyChart(chart) {
  if (!chart) return;
  try {
    chart.destroy();
  } catch {
    // Ignore
  }
  activeCharts.delete(chart);
}

function getChartCtor() {
  if (typeof window.Chart !== "function") {
    throw new Error("Chart.js failed to load from vendor/chart.umd.min.js");
  }
  return window.Chart;
}

export function createChart(canvas, config) {
  const ChartCtor = getChartCtor();
  const chart = new ChartCtor(canvas, config);
  activeCharts.add(chart);
  return chart;
}

export const PALETTE = {
  blue: "#2563eb",
  blueSoft: "rgba(37, 99, 235, 0.78)",
  indigo: "#4f46e5",
  indigoSoft: "rgba(79, 70, 229, 0.78)",
  violet: "#7c3aed",
  violetSoft: "rgba(124, 58, 237, 0.76)",
  emerald: "#059669",
  emeraldSoft: "rgba(5, 150, 105, 0.78)",
  amber: "#d97706",
  amberSoft: "rgba(217, 119, 6, 0.8)",
  rose: "#e11d48",
  roseSoft: "rgba(225, 29, 72, 0.78)",
  slate: "#64748b",
  slateSoft: "rgba(100, 116, 139, 0.65)",
  grid: "rgba(148, 163, 184, 0.22)",
  text: "#475569",
};

export function fmtAxisNum(value, maxDecimals = 2) {
  const num = Number(value);
  if (!Number.isFinite(num)) return String(value);
  const rounded = Number(num.toFixed(maxDecimals));
  return Object.is(rounded, -0) ? "0" : String(rounded);
}

export function fmtAxisPct(value) {
  return `${fmtAxisNum(value, 2)}%`;
}

export function fmtAxisUsdB(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) return String(value);
  const abs = Math.abs(num);
  const sign = num < 0 ? "-" : "";
  if (abs > 0 && abs < 0.01) {
    return `${sign}$${fmtAxisNum(abs * 1000, 1)}M`;
  }
  return `${sign}$${fmtAxisNum(abs, 2)}B`;
}

export function fmtAxisUsdM(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) return String(value);
  const sign = num < 0 ? "-" : "";
  return `${sign}$${fmtAxisNum(Math.abs(num), 1)}M`;
}

export function fmtAxisUsd(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) return String(value);
  const sign = num < 0 ? "-" : "";
  return `${sign}$${fmtAxisNum(Math.abs(num), 2)}`;
}

export function fmtAxisSignedInt(value) {
  const num = Math.round(Number(value));
  if (!Number.isFinite(num)) return String(value);
  return num > 0 ? `+${num}` : String(num);
}

export function isDarkTheme() {
  return document.documentElement?.dataset?.theme === "dark";
}

export function chartThemeColors() {
  if (isDarkTheme()) {
    return {
      grid: "rgba(161, 161, 170, 0.16)",
      text: "#a1a1aa",
      legend: "#d4d4d8",
      tooltipBg: "#27272a",
      tooltipTitle: "#fafafa",
      tooltipBody: "#e4e4e7",
    };
  }
  return {
    grid: "rgba(113, 113, 122, 0.15)",
    text: "#52525b",
    legend: "#27272a",
    tooltipBg: "#18181b",
    tooltipTitle: "#fafafa",
    tooltipBody: "#e4e4e7",
  };
}

export function baseChartOptions(yTickFormatter, rightYTickFormatter = null, extra = {}) {
  const theme = chartThemeColors();
  const scales = {
    x: {
      stacked: Boolean(extra.stacked),
      grid: { display: false },
      ticks: { color: theme.text, font: { size: 11 }, maxTicksLimit: 10 },
    },
    y: {
      position: "left",
      stacked: Boolean(extra.stacked),
      grid: { color: theme.grid },
      ticks: {
        color: theme.text,
        font: { size: 11 },
        maxTicksLimit: 6,
        callback: yTickFormatter,
      },
    },
  };

  if (rightYTickFormatter) {
    scales.y1 = {
      position: "right",
      grid: { drawOnChartArea: false },
      ticks: {
        color: theme.text,
        font: { size: 11 },
        maxTicksLimit: 6,
        callback: rightYTickFormatter,
      },
    };
  }

  return {
    responsive: true,
    maintainAspectRatio: false,
    interaction: { mode: "index", intersect: false },
    plugins: {
      legend: {
        position: "top",
        align: "start",
        labels: {
          boxWidth: 10,
          boxHeight: 10,
          useBorderRadius: true,
          borderRadius: 2,
          padding: 12,
          color: theme.legend,
          font: { size: 11 },
          generateLabels: (chart) => {
            const base = window.Chart.defaults.plugins.legend.labels.generateLabels(chart);
            for (const item of base) {
              // Prevent dashed datasets (borderDash) from rendering jagged legend borders
              item.lineDash = [];
              item.lineWidth = 0;
            }
            return base;
          },
        },
      },
      tooltip: {
        backgroundColor: theme.tooltipBg,
        titleColor: theme.tooltipTitle,
        bodyColor: theme.tooltipBody,
        padding: 10,
        cornerRadius: 4,
        ...(extra.tooltip || {}),
        callbacks: {
          label: (ctx) => {
            const raw = ctx.parsed?.y;
            if (raw === null || raw === undefined || !Number.isFinite(raw)) return null;
            const fmt =
              ctx.dataset?.yAxisID === "y1" && rightYTickFormatter
                ? rightYTickFormatter
                : yTickFormatter;
            const formatted = fmt ? fmt(raw) : fmtAxisNum(raw, 2);
            return ` ${ctx.dataset.label || ""}: ${formatted}`;
          },
          ...(extra.tooltip?.callbacks || {}),
        },
      },
    },
    scales,
  };
}
