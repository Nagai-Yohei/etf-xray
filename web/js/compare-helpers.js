/**
 * Pure ETF overlap calculation helpers (no DOM or network dependencies).
 *
 * Weight overlap uses the standard institutional definition:
 *   overlap_weight = sum_{s in A ∩ B} min(weight_A(s), weight_B(s))
 */

/**
 * Compute detailed pairwise overlap between two ETF holdings maps.
 * @param {Array<object>} holdingsA
 * @param {Array<object>} holdingsB
 * @param {Record<string, object>} stocksIndex
 */
export function computePairOverlap(holdingsA, holdingsB, stocksIndex = {}) {
  const mapA = new Map();
  for (const h of holdingsA || []) {
    mapA.set(h.security_id, h);
  }
  const mapB = new Map();
  for (const h of holdingsB || []) {
    mapB.set(h.security_id, h);
  }

  const common = [];
  const onlyA = [];
  const onlyB = [];

  let overlapWeight = 0;
  let sharedWeightA = 0;
  let sharedWeightB = 0;

  for (const [sid, hA] of mapA.entries()) {
    const hB = mapB.get(sid);
    const meta = stocksIndex[sid] || {};
    if (hB) {
      const wA = Number(hA.weight) || 0;
      const wB = Number(hB.weight) || 0;
      const minW = Math.min(wA, wB);
      overlapWeight += minW;
      sharedWeightA += wA;
      sharedWeightB += wB;
      common.push({
        security_id: sid,
        ticker: hA.ticker || hB.ticker || sid,
        name: hA.name || hB.name || meta.name || sid,
        weightA: wA,
        weightB: wB,
        diff: wA - wB,
        overlap: minW,
        rankA: hA.rank,
        rankB: hB.rank,
        m: meta,
      });
    } else {
      onlyA.push({
        security_id: sid,
        ticker: hA.ticker || sid,
        name: hA.name || meta.name || sid,
        weight: Number(hA.weight) || 0,
        rank: hA.rank,
        m: meta,
      });
    }
  }

  for (const [sid, hB] of mapB.entries()) {
    if (!mapA.has(sid)) {
      const meta = stocksIndex[sid] || {};
      onlyB.push({
        security_id: sid,
        ticker: hB.ticker || sid,
        name: hB.name || meta.name || sid,
        weight: Number(hB.weight) || 0,
        rank: hB.rank,
        m: meta,
      });
    }
  }

  common.sort((x, y) => y.overlap - x.overlap || y.weightA + y.weightB - (x.weightA + x.weightB));
  onlyA.sort((x, y) => y.weight - x.weight);
  onlyB.sort((x, y) => y.weight - x.weight);

  const countA = mapA.size;
  const countB = mapB.size;
  const unionCount = countA + countB - common.length;

  return {
    countA,
    countB,
    commonCount: common.length,
    onlyACount: onlyA.length,
    onlyBCount: onlyB.length,
    unionCount,
    jaccard: unionCount > 0 ? common.length / unionCount : 0,
    overlapWeight,
    sharedWeightA,
    sharedWeightB,
    uniqueWeightA: Math.max(0, 1 - sharedWeightA),
    uniqueWeightB: Math.max(0, 1 - sharedWeightB),
    common,
    onlyA,
    onlyB,
  };
}

/**
 * Compute full N x N pairwise overlap matrix across all ETFs.
 * @param {Array<string>} etfIds
 * @param {Record<string, Array<object>>} holdingsByEtf
 */
export function computeOverlapMatrix(etfIds, holdingsByEtf) {
  const maps = {};
  for (const eid of etfIds) {
    const m = new Map();
    for (const h of holdingsByEtf[eid] || []) {
      m.set(h.security_id, Number(h.weight) || 0);
    }
    maps[eid] = m;
  }

  const matrix = {};
  for (const a of etfIds) {
    matrix[a] = {};
    const mA = maps[a];
    for (const b of etfIds) {
      if (a === b) {
        matrix[a][b] = { overlapWeight: 1, commonCount: mA.size, self: true };
        continue;
      }
      const mB = maps[b];
      let ov = 0;
      let cnt = 0;
      for (const [sid, wA] of mA.entries()) {
        if (mB.has(sid)) {
          cnt += 1;
          ov += Math.min(wA, mB.get(sid));
        }
      }
      matrix[a][b] = { overlapWeight: ov, commonCount: cnt, self: false };
    }
  }
  return matrix;
}

/**
 * Compute stocks held simultaneously by >= minEtfs ETFs, sorted by ETF count then average weight.
 * @param {Array<string>} etfIds
 * @param {Record<string, Array<object>>} holdingsByEtf
 * @param {Record<string, object>} stocksIndex
 * @param {number} minEtfs
 */
export function computeCoreHoldings(etfIds, holdingsByEtf, stocksIndex = {}, minEtfs = 3) {
  const bySec = new Map();
  for (const eid of etfIds) {
    for (const h of holdingsByEtf[eid] || []) {
      const sid = h.security_id;
      let entry = bySec.get(sid);
      if (!entry) {
        const meta = stocksIndex[sid] || {};
        entry = {
          security_id: sid,
          ticker: h.ticker || meta.ticker || sid,
          name: h.name || meta.name || sid,
          etfs: [],
          weights: {},
          totalWeight: 0,
          m: meta,
        };
        bySec.set(sid, entry);
      }
      const w = Number(h.weight) || 0;
      entry.etfs.push(eid);
      entry.weights[eid] = w;
      entry.totalWeight += w;
    }
  }

  const result = [];
  for (const entry of bySec.values()) {
    if (entry.etfs.length >= minEtfs) {
      entry.etfCount = entry.etfs.length;
      entry.avgWeight = entry.totalWeight / entry.etfs.length;
      result.push(entry);
    }
  }

  result.sort((a, b) => b.etfCount - a.etfCount || b.avgWeight - a.avgWeight);
  return result;
}
