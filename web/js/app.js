/**
 * SPA エントリポイント・ハッシュルーター・グローバル銘柄検索。
 * ルート:
 *   #/etf/<ETF_ID>
 *   #/stock/<SECURITY_ID>
 */

import { getMeta, getStocksIndex } from "./api.js";
import { clearNode, el, fmtPct } from "./dom.js";
import { renderEtfView } from "./etf-view.js";
import { renderStockView } from "./stock-view.js";

const rootEl = document.getElementById("app-root");
const etfSwitcher = document.getElementById("etf-switcher");
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

let metaData = null;
let stocksList = [];
let currentEtfId = "SCHD";
let cleanupView = null;

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
            ? el("span", { className: "num text-muted", text: fmtPct(s.ttm_yield, 2) })
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
  if (typeof cleanupView === "function") {
    cleanupView();
    cleanupView = null;
  }

  const hash = window.location.hash || `#/etf/${encodeURIComponent(currentEtfId)}`;
  const [pathPart, queryPart] = hash.replace(/^#\/?/, "").split("?");
  const parts = pathPart.split("/").filter(Boolean);
  const params = new URLSearchParams(queryPart || "");

  try {
    if (parts[0] === "stock" && parts[1]) {
      const secId = decodeURIComponent(parts[1]);
      await renderStockView(rootEl, secId, currentEtfId, setTopbarContext, metaData?.etfs || []);
      window.scrollTo({ top: 0 });
    } else {
      const etfId = parts[0] === "etf" && parts[1] ? decodeURIComponent(parts[1]) : currentEtfId;
      const known = metaData.etfs.some((e) => e.etf_id === etfId);
      const targetEtf = known ? etfId : metaData.etfs[0].etf_id;
      currentEtfId = targetEtf;
      etfSwitcher.value = targetEtf;
      setTopbarContext(null);
      document.title = `${targetEtf} — ETF X-Ray`;
      cleanupView = await renderEtfView(rootEl, targetEtf, params.get("as_of"), navigate);
    }
  } catch (err) {
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
    stocksList = Object.values(stocksIndex);

    clearNode(etfSwitcher);
    for (const etf of meta.etfs) {
      etfSwitcher.appendChild(
        el("option", { value: etf.etf_id, text: `${etf.etf_id} — ${etf.name}` })
      );
    }
    if (meta.etfs.length > 0) {
      currentEtfId = meta.etfs[0].etf_id;
    }

    etfSwitcher.addEventListener("change", (e) => {
      currentEtfId = e.target.value;
      navigate(`#/etf/${encodeURIComponent(currentEtfId)}`);
    });

    if (footerEl && meta.disclaimer) {
      clearNode(footerEl);
      footerEl.appendChild(
        el("p", {
          className: "footer-note",
          text: meta.disclaimer,
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
