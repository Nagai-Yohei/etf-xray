/**
 * Split JSON fetcher, deterministic stock shard resolver, and in-memory cache.
 */

const jsonCache = new Map();
let fullIndexPromise = null;

async function fetchJson(path) {
  if (jsonCache.has(path)) {
    return jsonCache.get(path);
  }
  const promise = fetch(path, { credentials: "same-origin" }).then(async (res) => {
    if (!res.ok) {
      throw new Error(`Failed to load ${path} (HTTP ${res.status})`);
    }
    const ct = res.headers?.get?.("content-type") || "";
    if (ct.includes("text/html")) {
      throw new Error("Site data was updated — please reload the page.");
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

/**
 * Deterministic 2-hex-digit shard ID (FNV-1a 32-bit mod 256).
 * Matches `stock_shard_id` in `src/export_site.py` identically.
 */
export function stockShardId(securityId) {
  const s = String(securityId);
  let h = 0x811c9dc5;
  for (let i = 0; i < s.length; i += 1) {
    const code = s.charCodeAt(i);
    if (code < 0x80) {
      h ^= code;
      h = Math.imul(h, 0x01000193) >>> 0;
    } else if (code < 0x800) {
      h ^= 0xc0 | (code >> 6);
      h = Math.imul(h, 0x01000193) >>> 0;
      h ^= 0x80 | (code & 0x3f);
      h = Math.imul(h, 0x01000193) >>> 0;
    } else {
      h ^= 0xe0 | (code >> 12);
      h = Math.imul(h, 0x01000193) >>> 0;
      h ^= 0x80 | ((code >> 6) & 0x3f);
      h = Math.imul(h, 0x01000193) >>> 0;
      h ^= 0x80 | (code & 0x3f);
      h = Math.imul(h, 0x01000193) >>> 0;
    }
  }
  return (h & 0xff).toString(16).padStart(2, "0");
}

export function getMeta() {
  return fetchJson("data/meta.json");
}

export function getEtfSummary(etfId) {
  return fetchJson(`data/etf/${encodeURIComponent(etfId)}/summary.json`);
}

export function getEtfTenure(etfId) {
  return fetchJson(`data/etf/${encodeURIComponent(etfId)}/tenure.json`);
}

export function getEtfHoldings(etfId, asOf) {
  return fetchJson(
    `data/etf/${encodeURIComponent(etfId)}/holdings/${encodeURIComponent(asOf)}.json`
  );
}

export function getCompareOverview() {
  return fetchJson("data/compare/overview.json");
}

export function getSearchActiveIndex() {
  return fetchJson("data/stocks/search-active.json");
}

export function getActiveStocksIndex() {
  return fetchJson("data/stocks/index-active.json");
}

export function getFormerStocksIndex() {
  return fetchJson("data/stocks/index-former.json");
}

export function getStocksIndex() {
  if (!fullIndexPromise) {
    fullIndexPromise = Promise.all([
      getActiveStocksIndex(),
      getFormerStocksIndex(),
    ])
      .then(([active, former]) => ({ ...former, ...active }))
      .catch((err) => {
        fullIndexPromise = null;
        throw err;
      });
  }
  return fullIndexPromise;
}

export async function getStockDetail(securityId) {
  const shard = stockShardId(securityId);
  const bucket = await fetchJson(`data/stocks/shards/${shard}.json`);
  const detail = bucket?.[securityId];
  if (!detail) {
    throw new Error(`Stock not found: ${securityId}`);
  }
  return detail;
}
