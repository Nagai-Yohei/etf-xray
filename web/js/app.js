/**
 * SPA entry point, hash router, and global stock search.
 * Routes:
 *   #/                  (Home overview)
 *   #/etf/<ETF_ID>      (ETF holdings & history view)
 *   #/compare           (ETF overlap & comparison view)
 *   #/screener          (Cross-ETF stock screener)
 *   #/stock/<SECURITY_ID> (Individual stock fundamentals view)
 */

import { getMeta, getStocksIndex } from "./api.js";
import { renderCompareView } from "./compare-view.js";
import { clearNode, el, fmtPct } from "./dom.js";
import { renderEtfView } from "./etf-view.js";
import { renderHomeView } from "./home-view.js";
import { renderScreenerView } from "./screener-view.js";
import { renderStockView } from "./stock-view.js";

const rootEl = document.getElementById("app-root");
const etfSwitcher = document.getElementById("etf-switcher");
const navCompareLink = document.getElementById("nav-compare-link");
const navScreenerLink = document.getElementById("nav-screener-link");
const searchInput = document.getElementById("global-search-input");
const searchDropdown = document.getElementById("global-search-results");
const topbarContext = document.getElementById("topbar-context");
const footerEl = document.getElementById("app-footer");

function setTopbarContext(info) {
  if (!topbarContext) return;
  clearNode(topbarContext);
  if (!info) {
    topbarContext.classList.add("hidden");
    return;
  }
  topbarContext.appendChild(el("span", { className: "ticker-pill", text: info.ticker }));
  topbarContext.appendChild(el("span", { className: "topbar-context-name", text: info.name }));
  topbarContext.title = `${info.ticker} — ${info.name} (click to scroll to top)`;
  topbarContext.classList.remove("hidden");
}

if (topbarContext) {
  topbarContext.addEventListener("click", () => {
    window.scrollTo({ top: 0, behavior: "smooth" });
  });
}

const topbarEl = document.querySelector(".topbar");
let stickyRaf = 0;

/**
 * Keep table column headers and the tenure map year header visible below the sticky topbar
 * even when the outer window scrolls past the top of an overflow-scrollable container.
 */
function syncStickyHeaders() {
  stickyRaf = 0;
  const topbarBottom = topbarEl ? topbarEl.getBoundingClientRect().bottom : 0;

  for (const wrap of document.querySelectorAll(".table-scroll")) {
    const table = wrap.querySelector(".data-table");
    const thead = table?.querySelector("thead");
    if (!table || !thead) continue;
    const ths = thead.querySelectorAll("th");
    if (!ths.length) continue;

    const wrapRect = wrap.getBoundingClientRect();
    const tableRect = table.getBoundingClientRect();
    const headH = thead.offsetHeight || 34;

    let dy = 0;
    if (wrapRect.top < topbarBottom && wrapRect.bottom > topbarBottom + headH) {
      const rawOffset = topbarBottom - wrapRect.top;
      const visibleBottom = Math.min(wrapRect.bottom, tableRect.bottom);
      const maxOffset = Math.max(0, visibleBottom - wrapRect.top - headH);
      dy = Math.min(rawOffset, maxOffset);
    }

    const transformVal = dy > 0.5 ? `translateY(${Math.round(dy)}px)` : "";
    const isFloating = dy > 0.5;
    for (const th of ths) {
      if (th.style.transform !== transformVal) {
        th.style.transform = transformVal;
      }
      th.classList.toggle("sticky-floating", isFloating);
    }
  }

  for (const wrap of document.querySelectorAll(".tenure-canvas-wrap")) {
    const headerCanvas = wrap.querySelector(".tenure-header-canvas");
    if (!headerCanvas) continue;

    const wrapRect = wrap.getBoundingClientRect();
    const headH = headerCanvas.offsetHeight || 28;
    let dy = 0;
    if (wrapRect.top < topbarBottom && wrapRect.bottom > topbarBottom + headH) {
      const rawOffset = topbarBottom - wrapRect.top;
      const maxOffset = Math.max(0, wrapRect.bottom - wrapRect.top - headH);
      dy = Math.min(rawOffset, maxOffset);
    }

    const transformVal = dy > 0.5 ? `translateY(${Math.round(dy)}px)` : "";
    if (headerCanvas.style.transform !== transformVal) {
      headerCanvas.style.transform = transformVal;
    }
    headerCanvas.classList.toggle("sticky-floating", dy > 0.5);
  }
}

function scheduleStickySync() {
  if (!stickyRaf) {
    stickyRaf = window.requestAnimationFrame(syncStickyHeaders);
  }
}

window.addEventListener("scroll", scheduleStickySync, { passive: true });
window.addEventListener("resize", scheduleStickySync, { passive: true });

let metaData = null;
let stocksIndexMap = {};
let stocksList = [];
let currentEtfId = "SCHD";
let lastBackTarget = null;
let cleanupView = null;
let routeSeq = 0;

function navigate(hash) {
  if (window.location.hash === hash) {
    handleRoute();
  } else {
    window.location.hash = hash;
  }
}

function setupGlobalSearch() {
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
      .slice(0, 12);

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

        const yieldText =
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
              el("div", { className: "toolbar-group" }, [yieldText, etfTag]),
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
  document.addEventListener("click", (e) => {
    if (!e.target.closest("#global-search-wrap")) {
      closeDropdown();
    }
  });
}

async function handleRoute() {
  const mySeq = ++routeSeq;
  const isStale = () => mySeq !== routeSeq;

  if (typeof cleanupView === "function") {
    cleanupView();
    cleanupView = null;
  }

  const rawHash = window.location.hash || "#/";
  const [pathPart, queryPart] = rawHash.replace(/^#\/?/, "").split("?");
  const parts = pathPart.split("/").filter(Boolean);
  const params = new URLSearchParams(queryPart || "");
  if (navCompareLink) {
    navCompareLink.classList.toggle("active", parts[0] === "compare");
  }
  if (navScreenerLink) {
    navScreenerLink.classList.toggle("active", parts[0] === "screener");
  }

  try {
    let nextCleanup = null;
    if (parts.length === 0) {
      etfSwitcher.value = "";
      lastBackTarget = { href: "#/", label: "← Back to Home" };
      setTopbarContext(null);
      document.title = "ETF X-Ray — U.S. ETF Holdings & SEC Fundamentals";
      nextCleanup = await renderHomeView(rootEl, metaData, stocksList, navigate);
      if (isStale()) {
        if (typeof nextCleanup === "function") nextCleanup();
        return;
      }
      cleanupView = nextCleanup;
      window.scrollTo({ top: 0 });
    } else if (parts[0] === "compare") {
      etfSwitcher.value = "";
      const reqA = params.get("a") ? params.get("a").toUpperCase() : null;
      const reqB = params.get("b") ? params.get("b").toUpperCase() : null;
      setTopbarContext(null);
      nextCleanup = await renderCompareView(
        rootEl,
        metaData,
        stocksIndexMap,
        reqA,
        reqB,
        navigate,
        (a, b) => {
          if (isStale()) return;
          lastBackTarget = {
            href: `#/compare?a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}`,
            label: `← Back to Compare (${a} vs ${b})`,
          };
        },
        isStale
      );
      if (isStale()) {
        if (typeof nextCleanup === "function") nextCleanup();
        return;
      }
      cleanupView = nextCleanup;
      window.scrollTo({ top: 0 });
    } else if (parts[0] === "screener") {
      etfSwitcher.value = "";
      setTopbarContext(null);
      nextCleanup = await renderScreenerView(
        rootEl,
        metaData,
        stocksList,
        params,
        navigate,
        (hash) => {
          if (isStale()) return;
          lastBackTarget = {
            href: hash || "#/screener",
            label: "← Back to Screener",
          };
        }
      );
      if (isStale()) {
        if (typeof nextCleanup === "function") nextCleanup();
        return;
      }
      cleanupView = nextCleanup;
      window.scrollTo({ top: 0 });
    } else if (parts[0] === "stock" && parts[1]) {
      const secId = decodeURIComponent(parts[1]);
      await renderStockView(
        rootEl,
        secId,
        currentEtfId,
        setTopbarContext,
        metaData?.etfs || [],
        lastBackTarget,
        isStale
      );
      if (isStale()) return;
      window.scrollTo({ top: 0 });
    } else if (parts[0] === "etf") {
      const etfId = parts[1] ? decodeURIComponent(parts[1]) : currentEtfId;
      const known = metaData.etfs.some((e) => e.etf_id === etfId);
      const targetEtf = known ? etfId : metaData.etfs[0].etf_id;
      const requestedAsOf = params.get("as_of");
      currentEtfId = targetEtf;
      etfSwitcher.value = targetEtf;
      lastBackTarget = {
        href: requestedAsOf
          ? `#/etf/${encodeURIComponent(targetEtf)}?as_of=${encodeURIComponent(requestedAsOf)}`
          : `#/etf/${encodeURIComponent(targetEtf)}`,
        label: requestedAsOf ? `← Back to ${targetEtf} (${requestedAsOf})` : `← Back to ${targetEtf}`,
      };
      setTopbarContext(null);
      document.title = `${targetEtf} — ETF X-Ray`;
      nextCleanup = await renderEtfView(
        rootEl,
        targetEtf,
        requestedAsOf,
        navigate,
        (asOf, isLatest) => {
          if (isStale()) return;
          lastBackTarget = {
            href: isLatest
              ? `#/etf/${encodeURIComponent(targetEtf)}`
              : `#/etf/${encodeURIComponent(targetEtf)}?as_of=${encodeURIComponent(asOf)}`,
            label: isLatest ? `← Back to ${targetEtf}` : `← Back to ${targetEtf} (${asOf})`,
          };
        },
        isStale
      );
      if (isStale()) {
        if (typeof nextCleanup === "function") nextCleanup();
        return;
      }
      cleanupView = nextCleanup;
    } else {
      etfSwitcher.value = "";
      lastBackTarget = { href: "#/", label: "← Back to Home" };
      setTopbarContext(null);
      document.title = "ETF X-Ray — U.S. ETF Holdings & SEC Fundamentals";
      nextCleanup = await renderHomeView(rootEl, metaData, stocksList, navigate);
      if (isStale()) {
        if (typeof nextCleanup === "function") nextCleanup();
        return;
      }
      cleanupView = nextCleanup;
      window.scrollTo({ top: 0 });
    }
    scheduleStickySync();
  } catch (err) {
    if (isStale()) return;
    setTopbarContext(null);
    clearNode(rootEl);
    rootEl.appendChild(
      el("div", {
        className: "error-box",
        text: `Unable to render view: ${err.message || String(err)}`,
      })
    );
  }
}

async function init() {
  try {
    const [meta, stocksIndex] = await Promise.all([getMeta(), getStocksIndex()]);
    metaData = meta;
    stocksIndexMap = stocksIndex;
    stocksList = Object.values(stocksIndex);

    clearNode(etfSwitcher);
    etfSwitcher.appendChild(el("option", { value: "", text: "All ETFs (Home)" }));
    for (const etf of meta.etfs) {
      etfSwitcher.appendChild(
        el("option", { value: etf.etf_id, text: `${etf.etf_id} — ${etf.name}` })
      );
    }
    if (meta.etfs.length > 0) {
      currentEtfId = meta.etfs.some((e) => e.etf_id === "SCHD") ? "SCHD" : meta.etfs[0].etf_id;
    }

    etfSwitcher.addEventListener("change", (e) => {
      const val = e.target.value;
      if (!val) {
        navigate("#/");
        return;
      }
      currentEtfId = val;
      navigate(`#/etf/${encodeURIComponent(currentEtfId)}`);
    });

    if (footerEl && meta.disclaimer) {
      clearNode(footerEl);
      const updatedDate = meta.generated_at ? String(meta.generated_at).slice(0, 10) : null;
      footerEl.appendChild(
        el("p", {
          className: "footer-note",
          text: updatedDate ? `${meta.disclaimer} Last updated ${updatedDate} UTC.` : meta.disclaimer,
        })
      );
      footerEl.appendChild(
        el("p", {
          className: "footer-glossary",
          text:
            "Glossary: TTM = trailing twelve months · DPS = dividends per share · SBC = stock-based compensation · " +
            "Adj. FCF = FCF − SBC · QoQ = quarter-over-quarter weight change (percentage points) · " +
            "Div Streak = consecutive years without a dividend cut (+ indicates ongoing since earliest XBRL filing).",
        })
      );
    }

    setupGlobalSearch();
    window.addEventListener("hashchange", handleRoute);
    await handleRoute();
  } catch (err) {
    clearNode(rootEl);
    rootEl.appendChild(
      el("div", {
        className: "error-box",
        text: `Failed to initialize ETF X-Ray: ${err.message || String(err)}`,
      })
    );
  }
}

init();
