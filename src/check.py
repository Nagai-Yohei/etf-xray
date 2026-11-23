"""Data integrity check and secret/PII leak scan.

Usage:
    python -m src.check
"""

from __future__ import annotations

import getpass
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

from src.env import DB_PATH, DIST_DIR, ENV_PATH, PROJECT_ROOT, parse_dotenv_file

# Split pattern literals to avoid self-matching this module
PAT_PRIVATE_KEY = re.compile("-----BEGIN" + r" [A-Z0-9 ]*PRIVATE KEY-----")
PAT_GITHUB_TOKEN = re.compile(
    r"(?:gh[pousr]" + r"_[A-Za-z0-9]{20,}|github_" + r"pat_[A-Za-z0-9_]{20,})"
)
PAT_AWS_KEY = re.compile("AK" + r"IA[0-9A-Z]{16}")
PAT_GOOGLE_KEY = re.compile("AI" + r"za[0-9A-Za-z_-]{35}")
PAT_SLACK_TOKEN = re.compile("xo" + r"x[baprs]-[A-Za-z0-9-]{10,}")
PAT_SK_TOKEN = re.compile("s" + r"k-[A-Za-z0-9]{20,}")
PAT_ENV_SECRET = re.compile(
    r"(?:CLOUDFLARE_API_TOKEN|OPENFIGI_API_KEY|GITHUB_TOKEN|CF_API_TOKEN)"
    r"\s*=\s*['\"]?[A-Za-z0-9._-]{16,}"
)
PAT_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PAT_ALLOWED_EMAIL = re.compile(r"@example\.(?:com|org|net)$", re.IGNORECASE)
PAT_UNIX_PATH = re.compile(r"/(?:Users|home)/[A-Za-z0-9._-]+")
PAT_WIN_PATH = re.compile(r"[A-Za-z]:\\Users\\[A-Za-z0-9._-]+", re.IGNORECASE)
PAT_UNSAFE_DOM = re.compile(
    r"\b(?:inner" + r"HTML|outer" + r"HTML|insertAdjacent" + r"HTML)\b"
)
PAT_EXTERNAL_CDN = re.compile(
    r"https?://(?:cdn\.|cdnjs\.|unpkg\.com|cdn\.jsdelivr\.net|ajax\.googleapis\.com)",
    re.IGNORECASE,
)
PAT_JAPANESE = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff]")

EXCLUDED_TOP_DIRS = {".git", ".venv", "venv", "data", "dist", "__pycache__", ".wrangler"}
TEXT_SUFFIXES = {
    ".py",
    ".md",
    ".txt",
    ".json",
    ".csv",
    ".html",
    ".css",
    ".js",
    ".mjs",
    ".sh",
    ".yml",
    ".yaml",
    ".toml",
    ".example",
    ".gitignore",
}
TEXT_FILENAMES = {".gitignore", ".env.example", "_headers", "pre-commit", "run.sh"}


def _load_secret_values() -> list[str]:
    """Collect candidate secret values from env and .env without printing them."""
    candidates: list[str] = []
    dummy_pat = re.compile(r"@example\.(?:com|org|net)|your-name|changeme|dummy", re.IGNORECASE)

    def add(val: str) -> None:
        val = val.strip()
        if len(val) < 6 or dummy_pat.search(val):
            return
        if val not in candidates:
            candidates.append(val)

    for key in ("SEC_USER_AGENT", "OPENFIGI_API_KEY", "CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID", "GITHUB_TOKEN"):
        if os.environ.get(key):
            add(os.environ[key])

    for key, val in parse_dotenv_file(ENV_PATH).items():
        if key in ("SEC_USER_AGENT", "OPENFIGI_API_KEY", "CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID", "GITHUB_TOKEN"):
            add(val)

    return candidates


def _is_scannable_text(path: Path) -> bool:
    if path.name in TEXT_FILENAMES:
        return True
    return path.suffix.lower() in TEXT_SUFFIXES


def _iter_repo_files() -> list[Path]:
    """Return scannable repository files (excluding data/, dist/, .venv/, .git/)."""
    files: list[Path] = []
    for root, dirs, filenames in os.walk(PROJECT_ROOT):
        rel_root = Path(root).relative_to(PROJECT_ROOT)
        dirs[:] = [
            d
            for d in sorted(dirs)
            if not (rel_root == Path(".") and d in EXCLUDED_TOP_DIRS)
            and d != "__pycache__"
        ]
        for fname in sorted(filenames):
            fpath = Path(root) / fname
            rel = fpath.relative_to(PROJECT_ROOT)
            if rel.name == ".env" or (
                rel.name.startswith(".env.") and rel.name != ".env.example"
            ):
                continue
            if _is_scannable_text(fpath):
                files.append(fpath)
    return files


def _iter_dist_files() -> list[Path]:
    """Return scannable files under dist/."""
    if not DIST_DIR.is_dir():
        return []
    files: list[Path] = []
    for root, dirs, filenames in os.walk(DIST_DIR):
        dirs.sort()
        for fname in sorted(filenames):
            fpath = Path(root) / fname
            if _is_scannable_text(fpath):
                files.append(fpath)
    return files


def _scan_file_content(
    path: Path,
    rel_label: str,
    secrets: list[str],
    current_user: str,
    errors: list[str],
) -> None:
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return

    if PAT_PRIVATE_KEY.search(content):
        errors.append(f"[LEAK] Private key header detected: {rel_label}")
    if PAT_GITHUB_TOKEN.search(content):
        errors.append(f"[LEAK] GitHub token pattern detected: {rel_label}")
    if PAT_AWS_KEY.search(content):
        errors.append(f"[LEAK] AWS access key pattern detected: {rel_label}")
    if PAT_GOOGLE_KEY.search(content):
        errors.append(f"[LEAK] Google API key pattern detected: {rel_label}")
    if PAT_SLACK_TOKEN.search(content):
        errors.append(f"[LEAK] Slack token pattern detected: {rel_label}")
    if PAT_SK_TOKEN.search(content):
        errors.append(f"[LEAK] API secret key pattern detected: {rel_label}")
    if rel_label != ".env.example" and PAT_ENV_SECRET.search(content):
        errors.append(f"[LEAK] Hardcoded API token assignment detected: {rel_label}")

    for match in PAT_EMAIL.findall(content):
        if not PAT_ALLOWED_EMAIL.search(match):
            errors.append(f"[LEAK] Real email address detected: {rel_label}")
            break

    for match in PAT_UNIX_PATH.findall(content):
        if match not in ("/Users/...", "/home/..."):
            errors.append(f"[LEAK] Local absolute path detected: {rel_label}")
            break

    for match in PAT_WIN_PATH.findall(content):
        if not match.lower().endswith("\\users\\..."):
            errors.append(f"[LEAK] Windows local absolute path detected: {rel_label}")
            break

    if current_user and len(current_user) >= 3 and f"/Users/{current_user}" in content:
        errors.append(f"[LEAK] Path containing current local username detected: {rel_label}")

    for secret in secrets:
        if secret in content:
            errors.append(f"[LEAK] Secret value from env or .env detected: {rel_label}")
            break

    # Frontend security check (under web/ or dist/, excluding vendor/)
    parts = Path(rel_label).parts
    is_web_asset = (len(parts) >= 2 and parts[0] in ("web", "dist")) and "vendor" not in parts
    is_code_file = path.suffix.lower() in {".html", ".js", ".mjs", ".css"}
    if is_web_asset and is_code_file:
        if PAT_UNSAFE_DOM.search(content):
            errors.append(f"[SECURITY] Forbidden unsafe DOM API detected: {rel_label}")
        if PAT_EXTERNAL_CDN.search(content):
            errors.append(f"[SECURITY] External CDN reference detected: {rel_label}")

    # Enforce English-only repository source/config/docs (exclude vendored files and generated dist/)
    if parts and parts[0] != "dist" and "vendor" not in parts:
        if PAT_JAPANESE.search(content):
            errors.append(f"[I18N] Non-English (Japanese) characters detected: {rel_label}")


def check_git_tracked_files(errors: list[str]) -> int:
    """Verify no forbidden or ignored files are tracked in Git."""
    if not (PROJECT_ROOT / ".git").exists():
        return 0
    proc = subprocess.run(
        ["git", "-C", str(PROJECT_ROOT), "ls-files"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return 0

    tracked = [line for line in proc.stdout.splitlines() if line]
    for rel in tracked:
        if rel == ".env" or (rel.startswith(".env.") and rel != ".env.example"):
            errors.append(f"[GIT] Environment file is tracked in Git: {rel}")
        elif rel.startswith(("data/", "dist/", ".venv/", "venv/", ".wrangler/")):
            errors.append(f"[GIT] Ignored directory is tracked in Git: {rel}")
        elif rel.endswith((".db", ".sqlite", ".sqlite3", ".pem", ".key", ".p12", ".pfx")):
            errors.append(f"[GIT] Forbidden file is tracked in Git: {rel}")
    return len(tracked)


def check_database(errors: list[str]) -> str:
    """Validate integrity of data/market.db."""
    if not DB_PATH.is_file():
        return "skipped (DB not created yet)"

    conn = sqlite3.connect(str(DB_PATH))
    try:
        integrity = conn.execute("PRAGMA integrity_check;").fetchone()[0]
        if integrity != "ok":
            errors.append(f"[DB] SQLite integrity_check failed: {integrity}")

        # Verify required tables exist
        existing_tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table';"
            ).fetchall()
        }
        expected_tables = {
            "etfs",
            "etf_dates",
            "etf_holdings",
            "etf_changes",
            "securities",
            "quarter_prices",
            "financials_annual",
            "financials_quarterly",
            "financials_ttm",
            "stock_metrics",
        }
        missing = sorted(expected_tables - existing_tables)
        if missing:
            errors.append(f"[DB] Missing tables: {', '.join(missing)}")
            return "missing tables"

        # Verify holding weight sums are within tolerance
        bad_weights = conn.execute(
            """
            SELECT etf_id, as_of, SUM(weight) AS total_w
            FROM etf_holdings
            GROUP BY etf_id, as_of
            HAVING ABS(SUM(weight) - 1.0) > 0.02
            """
        ).fetchall()
        for etf_id, as_of, total_w in bad_weights:
            errors.append(
                f"[DB] Weight sum out of tolerance for {etf_id} {as_of}: {total_w:.4f}"
            )

        # Verify split factors in annual financials are positive
        bad_splits = conn.execute(
            """
            SELECT security_id, fy_end, split_factor
            FROM financials_annual
            WHERE split_factor IS NULL OR split_factor <= 0
            """
        ).fetchall()
        for sec_id, fy_end, sf in bad_splits:
            errors.append(f"[DB] Invalid split_factor for {sec_id} {fy_end}: {sf}")

        # Verify no ETF reporting date has median implied share price < $1.00 (catches missed $000 scaling)
        by_etf_date: dict[tuple[str, str], list[float]] = {}
        for etf_id, as_of, val_usd, shares in conn.execute(
            "SELECT etf_id, as_of, value_usd, shares FROM etf_holdings WHERE shares > 0 AND value_usd > 0"
        ).fetchall():
            by_etf_date.setdefault((etf_id, as_of), []).append(float(val_usd) / float(shares))
        for (etf_id, as_of), prices in sorted(by_etf_date.items()):
            prices.sort()
            med_price = prices[len(prices) // 2]
            if med_price < 1.0:
                errors.append(
                    f"[DB] Suspected $000 scaling bug for {etf_id} {as_of}: median price=${med_price:.4f}"
                )

        # Verify headline metrics are within sane bounds (catches 1,000x DPS or partial-revenue traps)
        bad_metrics = conn.execute(
            """
            SELECT security_id, ttm_yield, gross_margin
            FROM stock_metrics
            WHERE (ttm_yield IS NOT NULL AND (ttm_yield < 0 OR ttm_yield > 1.0))
               OR (gross_margin IS NOT NULL AND (gross_margin > 1.05 OR gross_margin < -2.0))
            """
        ).fetchall()
        for sec_id, yld, gm in bad_metrics:
            errors.append(
                f"[DB] Out-of-range metric for {sec_id}: ttm_yield={yld}, gross_margin={gm}"
            )

        # Verify no placeholder 0.0 values for share counts or total assets
        bad_zeros = conn.execute(
            """
            SELECT security_id, fy_end
            FROM financials_annual
            WHERE (shares_diluted IS NOT NULL AND shares_diluted <= 0)
               OR (shares_outstanding IS NOT NULL AND shares_outstanding <= 0)
               OR (total_assets IS NOT NULL AND total_assets <= 0)
            """
        ).fetchall()
        if bad_zeros:
            errors.append(
                f"[DB] Non-positive placeholder shares/assets in financials_annual: {len(bad_zeros)} row(s)"
            )

        # Verify EPS <= 0 latest annual rows never receive a positive payout_ratio_eps
        bad_neg_eps_payout = conn.execute(
            """
            SELECT m.security_id, m.payout_ratio_eps, a.eps_diluted
            FROM stock_metrics m
            JOIN financials_annual a ON a.security_id = m.security_id
            WHERE a.fy_end = (SELECT MAX(fy_end) FROM financials_annual WHERE security_id = m.security_id)
              AND a.eps_diluted IS NOT NULL AND a.eps_diluted <= 0
              AND m.payout_ratio_eps IS NOT NULL
            """
        ).fetchall()
        for sec_id, pr, eps in bad_neg_eps_payout:
            errors.append(
                f"[DB] payout_ratio_eps={pr} set despite non-positive EPS={eps} for {sec_id}"
            )

        # Verify warrants/preferreds/legacy .OLD entities do not carry common-stock yield/P/E
        bad_non_common = conn.execute(
            """
            SELECT security_id, ttm_yield, pe_ratio
            FROM stock_metrics
            WHERE (security_id LIKE '%.WS' OR security_id LIKE '%.WT' OR security_id LIKE '%.OLD')
              AND (ttm_yield IS NOT NULL OR pe_ratio IS NOT NULL)
            """
        ).fetchall()
        for sec_id, yld, pe in bad_non_common:
            errors.append(
                f"[DB] Non-common/legacy security {sec_id} has common-stock metrics (yield={yld}, pe={pe})"
            )

        sec_count = conn.execute("SELECT COUNT(*) FROM securities").fetchone()[0]
        ann_count = conn.execute("SELECT COUNT(*) FROM financials_annual").fetchone()[0]
        hold_count = conn.execute("SELECT COUNT(*) FROM etf_holdings").fetchone()[0]
        return f"OK (securities={sec_count}, financials_annual={ann_count}, etf_holdings={hold_count})"
    finally:
        conn.close()


def check_dist_json(errors: list[str]) -> str:
    """Verify all JSON files under dist/ parse cleanly and stock index cross-references exist."""
    if not DIST_DIR.is_dir():
        return "skipped (dist not built yet)"

    json_files = sorted(DIST_DIR.rglob("*.json"))
    for jf in json_files:
        try:
            json.loads(jf.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            rel = jf.relative_to(PROJECT_ROOT)
            errors.append(f"[DIST] Invalid JSON in {rel}: {exc}")

    # Verify every security_id in stocks/index.json has a corresponding detail JSON file
    stocks_index_path = DIST_DIR / "data" / "stocks" / "index.json"
    if stocks_index_path.is_file():
        try:
            idx_data = json.loads(stocks_index_path.read_text(encoding="utf-8"))
            if isinstance(idx_data, dict):
                stocks_dir = stocks_index_path.parent
                for sec_id in idx_data:
                    if not (stocks_dir / f"{sec_id}.json").is_file():
                        errors.append(f"[DIST] Missing stock detail JSON for {sec_id}")
        except Exception:  # noqa: BLE001
            pass

    return f"OK ({len(json_files)} JSON files)"


def main() -> int:
    errors: list[str] = []
    secrets = _load_secret_values()
    try:
        current_user = getpass.getuser()
    except Exception:  # noqa: BLE001
        current_user = ""

    tracked_count = check_git_tracked_files(errors)

    repo_files = _iter_repo_files()
    for fpath in repo_files:
        rel = str(fpath.relative_to(PROJECT_ROOT))
        _scan_file_content(fpath, rel, secrets, current_user, errors)

    dist_files = _iter_dist_files()
    for fpath in dist_files:
        rel = str(fpath.relative_to(PROJECT_ROOT))
        _scan_file_content(fpath, rel, secrets, current_user, errors)

    db_status = check_database(errors)
    dist_status = check_dist_json(errors)

    if errors:
        print(f"CHECK FAILED ({len(errors)} issue(s)):", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    print("CHECK OK:")
    print(f"  - Git tracked check: OK ({tracked_count} files)")
    print(f"  - Secret/PII scan: OK ({len(repo_files)} repo files, {len(dist_files)} dist files)")
    print(f"  - SQLite DB: {db_status}")
    print(f"  - Dist JSON: {dist_status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
