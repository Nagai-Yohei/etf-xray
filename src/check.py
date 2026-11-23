"""データ整合性検査 ＋ セキュリティ・情報流出スキャン。

実行方法:
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

# 自己マッチを避けるため、一部パターン文字列を分割して組み立てる
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
    """画面出力せずに環境変数・.env から実値候補を収集する。"""
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
    """リポジトリ内の検査対象ファイル一覧を返す（data/・dist/・.venv/・.git/ 除く）。"""
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
    """dist/ 配下の検査対象ファイル一覧を返す。"""
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
        errors.append(f"[LEAK] 秘密鍵ヘッダを検出: {rel_label}")
    if PAT_GITHUB_TOKEN.search(content):
        errors.append(f"[LEAK] GitHub トークン形式を検出: {rel_label}")
    if PAT_AWS_KEY.search(content):
        errors.append(f"[LEAK] AWS アクセスキー形式を検出: {rel_label}")
    if PAT_GOOGLE_KEY.search(content):
        errors.append(f"[LEAK] Google API キー形式を検出: {rel_label}")
    if PAT_SLACK_TOKEN.search(content):
        errors.append(f"[LEAK] Slack トークン形式を検出: {rel_label}")
    if PAT_SK_TOKEN.search(content):
        errors.append(f"[LEAK] API シークレットキー形式を検出: {rel_label}")
    if rel_label != ".env.example" and PAT_ENV_SECRET.search(content):
        errors.append(f"[LEAK] API トークン直接代入を検出: {rel_label}")

    for match in PAT_EMAIL.findall(content):
        if not PAT_ALLOWED_EMAIL.search(match):
            errors.append(f"[LEAK] 実メールアドレスを検出: {rel_label}")
            break

    for match in PAT_UNIX_PATH.findall(content):
        if match not in ("/Users/...", "/home/..."):
            errors.append(f"[LEAK] ローカル絶対パスを検出: {rel_label}")
            break

    for match in PAT_WIN_PATH.findall(content):
        if not match.lower().endswith("\\users\\..."):
            errors.append(f"[LEAK] Windows ローカル絶対パスを検出: {rel_label}")
            break

    if current_user and len(current_user) >= 3 and f"/Users/{current_user}" in content:
        errors.append(f"[LEAK] 現在のローカルユーザー名を含むパスを検出: {rel_label}")

    for secret in secrets:
        if secret in content:
            errors.append(f"[LEAK] 環境変数または .env の秘密実値を検出: {rel_label}")
            break

    # フロントエンド安全チェック（web/ または dist/ 配下、vendor/ は除外）
    parts = Path(rel_label).parts
    is_web_asset = (len(parts) >= 2 and parts[0] in ("web", "dist")) and "vendor" not in parts
    is_code_file = path.suffix.lower() in {".html", ".js", ".mjs", ".css"}
    if is_web_asset and is_code_file:
        if PAT_UNSAFE_DOM.search(content):
            errors.append(f"[SECURITY] 禁止された DOM API を検出: {rel_label}")
        if PAT_EXTERNAL_CDN.search(content):
            errors.append(f"[SECURITY] 外部 CDN 読み込みを検出: {rel_label}")


def check_git_tracked_files(errors: list[str]) -> int:
    """Git に禁止ファイルが誤って追跡されていないか検査する。"""
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
            errors.append(f"[GIT] 環境変数ファイルが Git 追跡されています: {rel}")
        elif rel.startswith(("data/", "dist/", ".venv/", "venv/", ".wrangler/")):
            errors.append(f"[GIT] 除外対象ディレクトリが Git 追跡されています: {rel}")
        elif rel.endswith((".db", ".sqlite", ".sqlite3", ".pem", ".key", ".p12", ".pfx")):
            errors.append(f"[GIT] 禁止ファイルが Git 追跡されています: {rel}")
    return len(tracked)


def check_database(errors: list[str]) -> str:
    """data/market.db の整合性を検査する。"""
    if not DB_PATH.is_file():
        return "skipped (DB not created yet)"

    conn = sqlite3.connect(str(DB_PATH))
    try:
        integrity = conn.execute("PRAGMA integrity_check;").fetchone()[0]
        if integrity != "ok":
            errors.append(f"[DB] SQLite integrity_check failed: {integrity}")

        # 必須テーブル存在チェック
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
            "financials_ttm",
            "stock_metrics",
        }
        missing = sorted(expected_tables - existing_tables)
        if missing:
            errors.append(f"[DB] Missing tables: {', '.join(missing)}")
            return "missing tables"

        # 構成比合計の検証（データがある場合）
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

        # 年次財務の分割係数が正であることを検証
        bad_splits = conn.execute(
            """
            SELECT security_id, fy_end, split_factor
            FROM financials_annual
            WHERE split_factor IS NULL OR split_factor <= 0
            """
        ).fetchall()
        for sec_id, fy_end, sf in bad_splits:
            errors.append(f"[DB] Invalid split_factor for {sec_id} {fy_end}: {sf}")

        sec_count = conn.execute("SELECT COUNT(*) FROM securities").fetchone()[0]
        ann_count = conn.execute("SELECT COUNT(*) FROM financials_annual").fetchone()[0]
        hold_count = conn.execute("SELECT COUNT(*) FROM etf_holdings").fetchone()[0]
        return f"OK (securities={sec_count}, financials_annual={ann_count}, etf_holdings={hold_count})"
    finally:
        conn.close()


def check_dist_json(errors: list[str]) -> str:
    """dist/ 配下の JSON ファイルが破損していないか検証する。"""
    if not DIST_DIR.is_dir():
        return "skipped (dist not built yet)"

    json_files = sorted(DIST_DIR.rglob("*.json"))
    for jf in json_files:
        try:
            json.loads(jf.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            rel = jf.relative_to(PROJECT_ROOT)
            errors.append(f"[DIST] Invalid JSON in {rel}: {exc}")
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
