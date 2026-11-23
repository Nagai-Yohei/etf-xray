# etf-xray

A static web app and Python data pipeline for exploring U.S. ETF constituent histories and stock fundamentals using public SEC EDGAR filings and OpenFIGI metadata.

Live site: https://etf-xray.pages.dev

## Covered ETFs

- VYM — Vanguard High Dividend Yield ETF (2011-01 – present)
- VOO — Vanguard S&P 500 ETF (2011-03 – present)
- SCHD — Schwab U.S. Dividend Equity ETF (2011-11 – present)
- HDV — iShares Core High Dividend ETF (2019-10 – present)

## Quick Start

Requires Python 3.10+ and Git (no Node.js needed).

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
git config core.hooksPath scripts
cp .env.example .env
```

Set `SEC_USER_AGENT` in `.env` to your app/name and contact email per SEC EDGAR fair-access guidelines. `.env`, `data/`, and `dist/` are gitignored.

## Usage

```
./run.sh build                      # Fetch SEC filings -> XBRL -> metrics -> export dist/
./run.sh build --skip-etf --offline # Fast offline rebuild from local cache
./run.sh export                     # Re-export dist/ from existing SQLite DB
./run.sh check                      # Run data integrity checks and secret/PII scan
./run.sh serve                      # Preview dist/ locally at http://127.0.0.1:8787/
./run.sh deploy                     # Upload dist/ to Cloudflare Pages via Direct Upload API
```

## Overview

- `config/` — Target ETF definitions (`etfs.json`) and curated ticker overrides (`manual_tickers.csv`)
- `src/` — EDGAR client, filing adapters (`N-PORT-P`, Schwab/Vanguard legacy), entity resolution, XBRL extractor, metrics, static JSON exporter, integrity checker, local server, and Cloudflare deployer
- `web/` — Vanilla HTML/CSS/ES Modules frontend with locally bundled Chart.js and strict CSP (`_headers`)

## Deployment

Set `CLOUDFLARE_API_TOKEN` (scoped to Cloudflare Pages: Edit), `CLOUDFLARE_ACCOUNT_ID`, and `CLOUDFLARE_PAGES_PROJECT` in `.env`, then run `./run.sh deploy`. Alternatively, run `./run.sh check` and upload `dist/` via Cloudflare Pages Direct Upload in the dashboard.

## Notes & License

Data is compiled from SEC EDGAR (`N-PORT-P`, `N-CSR`, `N-CSRS`, `N-Q`, `10-K`/`10-Q` XBRL CompanyFacts) and OpenFIGI. Quarter-end share prices are implied from reported ETF holdings (`value_usd / shares`). For informational purposes only; not investment advice.

Licensed under the [MIT License](LICENSE).
