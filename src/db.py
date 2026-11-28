"""SQLite database connection and schema initialization.

Defines the two-layer data model (ETF holdings layer and equity fundamentals layer).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from src.env import DB_PATH

SCHEMA_SQL = """
-- ============================================================
-- 1. ETF holdings layer
-- ============================================================
CREATE TABLE IF NOT EXISTS etfs (
    etf_id                TEXT PRIMARY KEY,
    name                  TEXT NOT NULL,
    issuer                TEXT NOT NULL,
    cik                   TEXT NOT NULL,
    series_id             TEXT,
    structure             TEXT NOT NULL CHECK (structure IN ('open_end', 'uit')),
    fiscal_year_end_month INTEGER,
    active                INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS etf_dates (
    etf_id          TEXT NOT NULL,
    as_of           TEXT NOT NULL,
    holdings_count  INTEGER NOT NULL,
    total_value_usd REAL NOT NULL,
    source          TEXT NOT NULL,
    accession       TEXT,
    PRIMARY KEY (etf_id, as_of),
    FOREIGN KEY (etf_id) REFERENCES etfs(etf_id)
);

CREATE TABLE IF NOT EXISTS etf_holdings (
    etf_id         TEXT NOT NULL,
    as_of          TEXT NOT NULL,
    security_id    TEXT NOT NULL,
    ticker         TEXT,
    reported_name  TEXT NOT NULL,
    shares         REAL NOT NULL,
    value_usd      REAL NOT NULL,
    weight         REAL NOT NULL,
    pct_net_assets REAL,
    rank           INTEGER NOT NULL,
    prev_weight    REAL,
    is_new         INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (etf_id, as_of, security_id),
    FOREIGN KEY (etf_id, as_of) REFERENCES etf_dates(etf_id, as_of),
    FOREIGN KEY (security_id) REFERENCES securities(security_id)
);

CREATE INDEX IF NOT EXISTS idx_etf_holdings_security
    ON etf_holdings (security_id, as_of);

CREATE TABLE IF NOT EXISTS etf_changes (
    etf_id      TEXT NOT NULL,
    as_of       TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('add', 'remove')),
    security_id TEXT NOT NULL,
    ticker      TEXT,
    name        TEXT NOT NULL,
    weight      REAL,
    PRIMARY KEY (etf_id, as_of, kind, security_id),
    FOREIGN KEY (etf_id, as_of) REFERENCES etf_dates(etf_id, as_of)
);

-- ============================================================
-- 2. Equity fundamentals layer
-- ============================================================
CREATE TABLE IF NOT EXISTS securities (
    security_id     TEXT PRIMARY KEY,
    ticker          TEXT,
    name            TEXT NOT NULL,
    cusip           TEXT,
    isin            TEXT,
    cik             TEXT,
    ticker_source   TEXT,
    first_seen      TEXT,
    last_seen       TEXT,
    sic             TEXT,
    sic_description TEXT,
    sector_group    TEXT
);

CREATE INDEX IF NOT EXISTS idx_securities_ticker ON securities (ticker);
CREATE INDEX IF NOT EXISTS idx_securities_cik ON securities (cik);

CREATE TABLE IF NOT EXISTS quarter_prices (
    security_id TEXT NOT NULL,
    as_of       TEXT NOT NULL,
    price_usd   REAL NOT NULL,
    source_etf  TEXT NOT NULL,
    PRIMARY KEY (security_id, as_of),
    FOREIGN KEY (security_id) REFERENCES securities(security_id)
);

CREATE TABLE IF NOT EXISTS financials_annual (
    security_id         TEXT NOT NULL,
    fy_end              TEXT NOT NULL,
    fy_start            TEXT,
    filed               TEXT,
    revenue             REAL,
    gross_profit        REAL,
    operating_income    REAL,
    pretax_income       REAL,
    net_income          REAL,
    rd_expense          REAL,
    sga_expense         REAL,
    interest_expense    REAL,
    income_tax          REAL,
    eps_diluted         REAL,
    shares_diluted      REAL,
    shares_outstanding  REAL,
    operating_cf        REAL,
    investing_cf        REAL,
    financing_cf        REAL,
    capex               REAL,
    fcf                 REAL,
    sbc                 REAL,
    da_expense          REAL,
    dividends_paid      REAL,
    buybacks            REAL,
    total_assets        REAL,
    current_assets      REAL,
    total_equity        REAL,
    current_liabilities REAL,
    cash                REAL,
    total_debt          REAL,
    goodwill            REAL,
    intangibles         REAL,
    dps_declared        REAL,
    dps_paid            REAL,
    split_factor        REAL NOT NULL DEFAULT 1.0,
    PRIMARY KEY (security_id, fy_end),
    FOREIGN KEY (security_id) REFERENCES securities(security_id)
);

CREATE TABLE IF NOT EXISTS financials_quarterly (
    security_id      TEXT NOT NULL,
    period_end       TEXT NOT NULL,
    period_start     TEXT,
    fp               TEXT,
    revenue          REAL,
    operating_income REAL,
    pretax_income    REAL,
    net_income       REAL,
    eps_diluted      REAL,
    operating_cf     REAL,
    fcf              REAL,
    PRIMARY KEY (security_id, period_end),
    FOREIGN KEY (security_id) REFERENCES securities(security_id)
);

CREATE TABLE IF NOT EXISTS financials_ttm (
    security_id        TEXT PRIMARY KEY,
    period_end         TEXT NOT NULL,
    revenue_ttm        REAL,
    operating_income_ttm REAL,
    net_income_ttm     REAL,
    eps_ttm            REAL,
    fcf_ttm            REAL,
    dps_ttm            REAL,
    total_equity       REAL,
    shares_outstanding REAL,
    FOREIGN KEY (security_id) REFERENCES securities(security_id)
);

CREATE TABLE IF NOT EXISTS split_events (
    security_id TEXT NOT NULL,
    filed       TEXT NOT NULL,
    ratio       REAL NOT NULL,
    PRIMARY KEY (security_id, filed, ratio),
    FOREIGN KEY (security_id) REFERENCES securities(security_id)
);

CREATE TABLE IF NOT EXISTS stock_metrics (
    security_id       TEXT PRIMARY KEY,
    latest_price_date TEXT,
    latest_price      REAL,
    ttm_yield         REAL,
    payout_ratio_eps  REAL,
    payout_ratio_fcf  REAL,
    div_streak_years  INTEGER,
    div_streak_open   INTEGER,
    div_cagr_5y       REAL,
    div_cagr_10y      REAL,
    rev_cagr_5y       REAL,
    gross_margin      REAL,
    op_margin         REAL,
    roe               REAL,
    roe_5y_median     REAL,
    roic              REAL,
    equity_ratio      REAL,
    fcf_margin        REAL,
    fcf_ex_sbc_margin REAL,
    fcf_cagr_5y       REAL,
    fcf_positive_years INTEGER,
    net_debt_to_fcf   REAL,
    interest_coverage REAL,
    current_ratio     REAL,
    share_change_5y   REAL,
    pe_ratio          REAL,
    pb_ratio          REAL,
    data_notes        TEXT,
    FOREIGN KEY (security_id) REFERENCES securities(security_id)
);
"""


def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    """Open a SQLite connection."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    return conn


def init_db(db_path: Path = DB_PATH) -> sqlite3.Connection:
    """Create all tables (including additive column migrations) and return a connection."""
    conn = connect(db_path)
    conn.executescript(SCHEMA_SQL)

    # Safely add any missing columns to an existing database
    migrations: dict[str, tuple[tuple[str, str], ...]] = {
        "securities": (
            ("sic", "TEXT"),
            ("sic_description", "TEXT"),
            ("sector_group", "TEXT"),
        ),
        "financials_annual": (
            ("gross_profit", "REAL"),
            ("pretax_income", "REAL"),
            ("rd_expense", "REAL"),
            ("sga_expense", "REAL"),
            ("interest_expense", "REAL"),
            ("income_tax", "REAL"),
            ("investing_cf", "REAL"),
            ("financing_cf", "REAL"),
            ("sbc", "REAL"),
            ("da_expense", "REAL"),
            ("current_assets", "REAL"),
            ("current_liabilities", "REAL"),
            ("goodwill", "REAL"),
            ("intangibles", "REAL"),
        ),
        "stock_metrics": (
            ("gross_margin", "REAL"),
            ("roe_5y_median", "REAL"),
            ("roic", "REAL"),
            ("fcf_ex_sbc_margin", "REAL"),
            ("fcf_cagr_5y", "REAL"),
            ("fcf_positive_years", "INTEGER"),
            ("net_debt_to_fcf", "REAL"),
            ("interest_coverage", "REAL"),
            ("current_ratio", "REAL"),
            ("share_change_5y", "REAL"),
        ),
    }
    for table, cols in migrations.items():
        existing_cols = {
            row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for col_name, col_type in cols:
            if col_name not in existing_cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}")

    conn.commit()
    return conn
