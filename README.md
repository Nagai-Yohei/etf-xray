# etf-xray

A static web app and Python data pipeline for exploring U.S. ETF constituent histories and stock fundamentals using public SEC EDGAR filings and OpenFIGI metadata.

Live site: https://etf-xray.pages.dev

## Covered ETFs

- VYM — Vanguard High Dividend Yield ETF (2011-01 – present)
- VIG — Vanguard Dividend Appreciation ETF (2011-01 – present)
- DGRO — iShares Core Dividend Growth ETF (2014-07 – present)
- VOO — Vanguard S&P 500 ETF (2011-03 – present)
- SCHD — Schwab U.S. Dividend Equity ETF (2011-11 – present)
- HDV — iShares Core High Dividend ETF (2011-04 – present)
- SDY — SPDR S&P Dividend ETF (2011-03 – present)
- SPYD — SPDR Portfolio S&P 500 High Dividend ETF (2015-12 – present)
- DIA — SPDR Dow Jones Industrial Average ETF Trust (2011-04 – present; semi-annual `N-30D` through 2019-04, quarterly `N-PORT-P` from 2019-10)
- QQQ — Invesco QQQ Trust, Series 1 (2011-09 – present; annual `N-30B-2` through 2018-09, quarterly `N-PORT-P` from 2019-09)

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
./run.sh test                       # Run unit test suite (tests/)
./run.sh check                      # Run unit tests, data integrity checks, and secret/PII scan
./run.sh refresh                    # Incremental SEC check (N-PORT-P + stale active XBRL -> check; add --deploy to publish if changed)
./run.sh refresh --full             # Full pipeline rebuild + check
./run.sh schedule install           # Install local macOS launchd weekly refresh (check-only by default; add --deploy to auto-publish)
./run.sh schedule status            # Inspect launchd status and latest refresh summary log
./run.sh serve                      # Preview dist/ locally at http://127.0.0.1:8787/
./run.sh deploy                     # Upload dist/ to Cloudflare Pages via Direct Upload API
```

## Periodic Updates (macOS `launchd`)

`./run.sh refresh` runs incrementally by default: it checks SEC EDGAR for newly filed or amended `N-PORT-P` quarters without re-parsing immutable 2011–2019 legacy HTML reports, refreshes active securities' XBRL `CompanyFacts` older than 14 days (`--xbrl-max-age-days`), aborts safely before database mutation if an unresolved ticker appears, and skips rebuild/export/deploy when no upstream changes are detected. Refresh summaries are written to `data/logs/refresh-YYYY-MM-DD.log`.

To schedule automated local runs on macOS without storing credentials or absolute paths in the repository:

```bash
./run.sh schedule install                    # Sunday 07:00 local time, check-only
./run.sh schedule install --deploy           # Sunday 07:00 local time, auto-deploy after src.check passes
./run.sh schedule status                     # Check registration and tail the latest refresh log
./run.sh schedule uninstall                  # Unload and remove the LaunchAgent
```

## Overview

- `config/` — Target ETF definitions (`etfs.json`), curated ticker overrides (`manual_tickers.csv`), and CIK chains (`cik_overrides.json`)
- `src/` — EDGAR client, filing adapters (`N-PORT-P`, Schwab/Vanguard/iShares/SPDR legacy), entity resolution, modular XBRL extractor (`src/fundamentals.py` facade + `src/xbrl/`), sector classification (`src/sectors.py`), metrics, static JSON exporter, integrity checker, local server, and Cloudflare deployer
- `tests/` — Standard-library `unittest` regression suite covering XBRL extraction, metrics, entity resolution, and sector rules
- `web/` — Vanilla HTML/CSS/ES Modules frontend with locally bundled Chart.js and strict CSP (`_headers`)

## Deployment

Set `CLOUDFLARE_API_TOKEN` (scoped to Cloudflare Pages: Edit), `CLOUDFLARE_ACCOUNT_ID`, and `CLOUDFLARE_PAGES_PROJECT` in `.env`, then run `./run.sh deploy`. Alternatively, run `./run.sh check` and upload `dist/` via Cloudflare Pages Direct Upload in the dashboard.

## Notes & License

Data is compiled from SEC EDGAR (`N-PORT-P`, `N-CSR`, `N-CSRS`, `N-Q`, `N-30D`, `10-K`/`10-Q` XBRL CompanyFacts) and OpenFIGI. Quarter-end share prices are implied from reported ETF holdings (`value_usd / shares`). Known SEC N-Q to N-PORT transition filing gaps (no public filing exists for those period ends): `SCHD 2019-05-31`, `VOO 2019-03-31`, `VYM 2019-07-31`, `VIG 2019-04-30`, `HDV 2019-07-31`, `DGRO 2019-07-31`, `SDY 2019-03-31`, and `SPYD 2019-03-31`. For informational purposes only; not investment advice.

Licensed under the [MIT License](LICENSE).
