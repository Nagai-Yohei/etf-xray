# AGENTS.md — etf-xray

Working rules for AI coding agents and maintainers. Read this file completely before making changes.

- Keep code, comments (new/updated), documentation, identifiers, and **all Web UI copy in English** (match the user's language in chat responses).
- Keep changes minimal and focused. After every change, run `./run.sh check` (`python -m src.check`) and confirm all checks pass.

---

## 1. Security & Leak-Prevention Rules (Strict)

1. **Never write secrets or personal information into code, configs, artifacts, or logs**
   - Keep `SEC_USER_AGENT` (name/email), `OPENFIGI_API_KEY`, `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, and any other credentials strictly in environment variables or `.env` (gitignored).
   - Never print, log, or copy the contents of `.env`.
   - Never embed local absolute paths (`/Users/...`, `C:\Users\...`) or personal email addresses in code, comments, JSON, or HTML.
2. **External / publishing operations**
   - Only run `git commit` / `git push` when **explicitly instructed by the user** and after both `./run.sh check` and `scripts/pre-commit` pass. Never auto-push.
   - Only run Cloudflare Pages deployments (`./run.sh deploy`) when **explicitly instructed by the user** and after `./run.sh check` passes. Never change repository visibility or create external repositories automatically.
3. **100% public data policy**
   - Allowed data sources are **SEC EDGAR** and **OpenFIGI** (plus `config/manual_tickers.csv`).
   - Do not add unofficial or redistribution-restricted APIs (e.g., Yahoo Finance).
4. **Frontend security rules**
   - Vanilla HTML / CSS / ES Modules JS only (no Node.js / npm build dependencies).
   - Do not load scripts from external CDNs; bundle chart libraries locally under `web/vendor/`.
   - Prevent XSS: **never use `innerHTML`, `outerHTML`, or `insertAdjacentHTML`** (use `textContent` and safe DOM APIs via `web/js/dom.js`).
   - Do not weaken the `Content-Security-Policy` in `web/_headers`.

---

## 2. Fragile Areas (EDGAR / XBRL / Entity Resolution)

- **EDGAR client (`src/edgar.py`)**:
  - Rate-limited to `<= 5 req/s`, cached under `data/raw/edgar/`, with exponential backoff on `429`/`5xx`.
  - Fails fast with an explicit error if `SEC_USER_AGENT` is missing or placeholder.
- **N-PORT-P adapter (`src/adapters/nport.py`)**:
  - Extract common equity long positions only (`assetCat == "EC"` and `payoffProfile == "Long"`), excluding MMFs, futures, and securities-lending collateral.
  - Validate 9-character alphanumeric CUSIPs and 12-character alphanumeric ISINs (reject `"000000000"`, `"N/A"`).
- **Legacy HTML adapters (`src/adapters/schwab_legacy.py`, `src/adapters/vanguard_legacy.py`, `src/adapters/ishares_legacy.py`, `src/adapters/spdr_legacy.py`, `src/adapters/invesco_legacy.py`)**:
  - Never disable the total-value reconciliation check against `Total Common Stock(s)`.
  - Known 2019 SEC N-Q → N-PORT transition filing gaps (no public filing exists): `SCHD 2019-05-31`, `VOO 2019-03-31`, `VYM 2019-07-31`, `VIG 2019-04-30`, `HDV 2019-07-31`, `DGRO 2019-07-31`, `SDY 2019-03-31`, `SPYD 2019-03-31`.
  - Schwab legacy: parse full "Portfolio Holdings as of ..." tables only (skip Condensed top-50 schedules).
  - Vanguard legacy: exclude non-target funds (e.g., International High Dividend Yield Index Fund), handle multi-line wrapped names, inline kerning tags, merged cells, and certification-only `/A` amendment fallbacks.
  - SPDR legacy (`src/adapters/spdr_legacy.py`): UIT `DIA` `N-30D` semi-annual Apr/Oct shareholder reports through `2019-04-30` (quarterly `N-PORT-P` from `2019-10-31`), plus SPDR Series Trust multi-fund `N-CSR`/`N-CSRS`/`N-Q` (`SDY` `2011-03-31` and `SPYD` `2015-12-31` through `2019-06-30`, quarterly `N-PORT-P` from `2019-09-30`).
  - Invesco UIT legacy (`QQQ` `N-30B-2`): annual Sep shareholder reports through `2018-09-30` (supporting both 2011–2015 and 2016–2018 column layouts), transitioning to quarterly `N-PORT-P` from `2019-09-30`.
- **Entity resolution (`src/identify.py`)**:
  - Priority: `manual_tickers.csv` (CUSIP) -> OpenFIGI (CUSIP/ISIN) -> legacy name bridge (unambiguous N-PORT names only) -> `manual_tickers.csv` (name) -> SEC `company_tickers.json`.
  - Raise an exception if duplicate `security_id`s appear within the same `(etf_id, as_of)`.
  - Disambiguate dual-class shares (e.g., `GOOGL`/`GOOG`, `BRK.A`/`BRK.B`, `CWEN`/`CWEN.A`) via CUSIP or exact manual overrides.
- **SEC XBRL (`src/fundamentals.py`, `src/xbrl/`, `src/metrics.py`, `src/sectors.py`)**:
  - `src/fundamentals.py` is the public facade over `src/xbrl/` (`tags.py`, `records.py`, `splits.py`, `annual.py`, `quarterly.py`).
  - Infer split factors across overlapping 10-K/10-Q periods so historical per-share metrics (`EPS`, `DPS`) stay split-adjusted.
  - Compute payout ratios on a same-fiscal-year basis (`DPS / EPS`, `DividendsPaid / NetIncome`, `DividendsPaid / FCF`) and suppress sector-distorted ratios (REIT GAAP payouts, bank/insurer ROIC/FCF) using official SEC SIC codes (`src/sectors.py`).
  - Derive implied quarter-end prices from ETF holdings (`value_usd / shares`).

---

## 3. Mandatory Post-Change Verification

1. Run `./run.sh check` and confirm all checks pass (Git tracked files, secret/PII scan, `tests/` unit tests, SQLite integrity, and `dist/` JSON validation).
2. After Web UI changes, serve locally with `./run.sh serve` and verify both ETF View (column customization, date switching, row navigation) and Stock View (annual/quarterly charts, tables, ETF ownership history) in a browser.
