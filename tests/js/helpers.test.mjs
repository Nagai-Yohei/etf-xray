/**
 * Zero-dependency frontend helper unit tests (runs on Node.js built-in node:test in CI).
 */

import assert from "node:assert/strict";
import test from "node:test";

import { stockShardId } from "../../web/js/api.js";
import {
  computeCoreHoldings,
  computeOverlapMatrix,
  computePairOverlap,
} from "../../web/js/compare-helpers.js";
import {
  buildScreenerCsv,
  computeScreenerSummary,
  createFilters,
  filterStocks,
  parseScreenerQuery,
  serializeScreenerQuery,
  sortStocks,
} from "../../web/js/screener-helpers.js";

test("stockShardId matches deterministic FNV-1a Python shards", () => {
  assert.equal(stockShardId("MSFT"), "73");
  assert.equal(stockShardId("AAPL").length, 2);
  assert.match(stockShardId("BRK.B"), /^[0-9a-f]{2}$/);
});

test("buildScreenerCsv preserves negative numbers while escaping formulas", () => {
  const csv = buildScreenerCsv(
    [
      {
        ticker: "AAPL",
        name: "=CMD|calc",
        current_etfs: ["VOO", "VTI"],
        sector_group: null,
        share_change_5y: -0.1523,
        fcf_cagr_5y: 0.1234,
      },
      {
        ticker: "BAD",
        name: "-SUM(A1:A2)",
        current_etfs: [],
        sector_group: "bank",
        share_change_5y: -0.05,
        fcf_cagr_5y: undefined,
      },
    ],
    [
      { key: "share_change_5y", label: "5Y Share Δ", csvPct: true, get: (r) => r.share_change_5y },
      { key: "fcf_cagr_5y", label: "5Y FCF CAGR", csvPct: true, get: (r) => r.fcf_cagr_5y },
    ]
  );

  const lines = csv.split("\r\n");
  assert.equal(lines.length, 3);
  assert.ok(lines[1].includes("'=CMD|calc"));
  assert.ok(lines[1].includes(",-15.23,12.34"));
  assert.ok(lines[2].includes("'-SUM(A1:A2)"));
  assert.ok(lines[2].includes(",-5.00,"));
});

test("screener filtering, sorting, summary, and URL serialization round-trip", () => {
  const stocks = [
    {
      security_id: "KO",
      ticker: "KO",
      name: "Coca-Cola Co",
      current_etfs: ["SCHD", "VOO", "VTI"],
      all_etfs: ["SCHD", "VOO", "VTI"],
      ttm_yield: 0.031,
      div_streak_years: 16,
      pe_ratio: 22,
      roic: 0.16,
    },
    {
      security_id: "OLD",
      ticker: "OLD",
      name: "Former Co",
      current_etfs: [],
      all_etfs: ["VTI"],
      ttm_yield: undefined,
      pe_ratio: undefined,
    },
  ];

  const activeOnly = filterStocks(stocks, createFilters({ scope: "active", minYield: "2.5" }));
  assert.deepEqual(
    activeOnly.map((s) => s.security_id),
    ["KO"]
  );

  const allScoped = filterStocks(stocks, createFilters({ scope: "all" }));
  assert.equal(allScoped.length, 2);

  const summary = computeScreenerSummary(allScoped);
  assert.equal(summary.count, 2);
  assert.equal(summary.dividendPayerCount, 1);
  assert.equal(summary.medianPe, 22);

  const sorted = sortStocks(stocks, "etf_count", -1);
  assert.equal(sorted[0].security_id, "KO");

  const hash = serializeScreenerQuery({
    filters: createFilters({ scope: "all", etfs: ["SCHD", "VOO"], minYield: "3" }),
    sortKey: "ttm_yield",
    sortDir: 1,
    colSet: "Dividend",
  });
  const parsed = parseScreenerQuery(new URLSearchParams(hash.split("?")[1]), ["SCHD", "VOO"]);
  assert.equal(parsed.filters.scope, "all");
  assert.deepEqual(parsed.filters.etfs, ["SCHD", "VOO"]);
  assert.equal(parsed.filters.minYield, "3");
  assert.equal(parsed.sortKey, "ttm_yield");
  assert.equal(parsed.sortDir, 1);
  assert.equal(parsed.colSet, "Dividend");
});

test("compare helpers compute pairwise overlap, matrix, and core holdings", () => {
  const holdingsByEtf = {
    AAA: [
      { security_id: "MSFT", ticker: "MSFT", name: "Microsoft", weight: 0.6, rank: 1 },
      { security_id: "KO", ticker: "KO", name: "Coca-Cola", weight: 0.4, rank: 2 },
    ],
    BBB: [
      { security_id: "MSFT", ticker: "MSFT", name: "Microsoft", weight: 0.25, rank: 1 },
      { security_id: "JNJ", ticker: "JNJ", name: "J&J", weight: 0.75, rank: 2 },
    ],
  };

  const pair = computePairOverlap(holdingsByEtf.AAA, holdingsByEtf.BBB, {});
  assert.equal(pair.commonCount, 1);
  assert.equal(pair.onlyACount, 1);
  assert.equal(pair.onlyBCount, 1);
  assert.ok(Math.abs(pair.overlapWeight - 0.25) < 1e-9);

  const mat = computeOverlapMatrix(["AAA", "BBB"], holdingsByEtf);
  assert.equal(mat.AAA.AAA.self, true);
  assert.equal(mat.AAA.AAA.commonCount, 2);
  assert.ok(Math.abs(mat.AAA.BBB.overlapWeight - 0.25) < 1e-9);

  const core = computeCoreHoldings(["AAA", "BBB"], holdingsByEtf, {}, 2);
  assert.deepEqual(
    core.map((c) => c.security_id),
    ["MSFT"]
  );
});
