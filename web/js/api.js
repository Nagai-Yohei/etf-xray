/**
 * 分割 JSON フェッチおよびメモリキャッシュ。
 */

const jsonCache = new Map();

async function fetchJson(path) {
  if (jsonCache.has(path)) {
    return jsonCache.get(path);
  }
  const promise = fetch(path, { credentials: "same-origin" }).then(async (res) => {
    if (!res.ok) {
      throw new Error(`Failed to load ${path} (HTTP ${res.status})`);
    }
    return res.json();
  });
  jsonCache.set(path, promise);
  try {
    return await promise;
  } catch (err) {
    jsonCache.delete(path);
    throw err;
  }
}

export function getMeta() {
  return fetchJson("data/meta.json");
}

export function getEtfSummary(etfId) {
  return fetchJson(`data/etf/${encodeURIComponent(etfId)}/summary.json`);
}

export function getEtfHoldings(etfId, asOf) {
  return fetchJson(
    `data/etf/${encodeURIComponent(etfId)}/holdings/${encodeURIComponent(asOf)}.json`
  );
}

export function getStocksIndex() {
  return fetchJson("data/stocks/index.json");
}

export function getStockDetail(securityId) {
  return fetchJson(`data/stocks/${encodeURIComponent(securityId)}.json`);
}
