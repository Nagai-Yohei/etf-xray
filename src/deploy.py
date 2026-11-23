"""Cloudflare Pages Direct Upload deployment script (Pure Python; no Node.js or wrangler).

Examples:
    python -m src.deploy             # Run src.check -> differentially upload dist/ to Cloudflare Pages
    python -m src.deploy --dry-run   # Pre-flight validation and asset summary only (no network)

Required environment variables (set in .env, gitignored):
    CLOUDFLARE_API_TOKEN     Cloudflare API token (permission: Account / Cloudflare Pages: Edit only)
    CLOUDFLARE_ACCOUNT_ID    Cloudflare account ID (32-character hex string)
    CLOUDFLARE_PAGES_PROJECT Target project name (default: etf-xray)

Security principles:
- Never log or include raw tokens, JWTs, or account IDs in output or exception messages.
- Always run src.check before deploying to prevent secret leaks or corrupted data.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import mimetypes
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from src import check as integrity_check
from src.env import DIST_DIR, load_dotenv

CF_API_BASE = "https://api.cloudflare.com/client/v4"
DEFAULT_PROJECT_NAME = "etf-xray"
# Max size and file count per upload batch (conservative safety margin under Cloudflare limits)
MAX_BATCH_BYTES = 8 * 1024 * 1024
MAX_BATCH_FILES = 150
MAX_FILE_BYTES = 25 * 1024 * 1024
# Cloudflare Pages special config files (sent as multipart fields instead of the asset manifest)
SPECIAL_CONFIG_FILES = {"_headers", "_redirects"}
IGNORED_FILENAMES = {".DS_Store", "Thumbs.db"}

EXTRA_MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
    ".xml": "application/xml; charset=utf-8",
    ".woff2": "font/woff2",
}


@dataclass
class AssetFile:
    """Single static asset file staged for upload."""

    rel_url: str  # e.g. "/index.html"
    path: Path
    size: int
    content_type: str
    content_hash: str  # 32-char lowercase hex hash


def _guess_content_type(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in EXTRA_MIME_TYPES:
        return EXTRA_MIME_TYPES[ext]
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


def _compute_asset_hash(raw_bytes: bytes, ext_without_dot: str) -> str:
    """Compute a deterministic 32-char Wrangler-compatible content hash (base64 + extension)."""
    b64_text = base64.b64encode(raw_bytes).decode("ascii")
    payload = (b64_text + ext_without_dot).encode("utf-8")
    return hashlib.blake2b(payload, digest_size=16).hexdigest()


def collect_dist_files(dist_dir: Path = DIST_DIR) -> tuple[list[AssetFile], dict[str, bytes]]:
    """Collect static assets and special config files (e.g. _headers) under dist/."""
    if not dist_dir.is_dir():
        raise RuntimeError(f"dist directory not found: {dist_dir}")

    required = ("index.html", "_headers", "data/meta.json")
    for rel in required:
        if not (dist_dir / rel).is_file():
            raise RuntimeError(f"Required build artifact missing in dist/: {rel}")

    assets: list[AssetFile] = []
    special_files: dict[str, bytes] = {}

    for path in sorted(dist_dir.rglob("*")):
        if not path.is_file():
            continue
        if path.name in IGNORED_FILENAMES or path.name.startswith("._"):
            continue
        rel = path.relative_to(dist_dir)
        rel_posix = rel.as_posix()

        raw = path.read_bytes()
        if len(raw) > MAX_FILE_BYTES:
            raise RuntimeError(
                f"File exceeds Cloudflare Pages 25 MiB limit: {rel_posix} ({len(raw)} bytes)"
            )

        if len(rel.parts) == 1 and path.name in SPECIAL_CONFIG_FILES:
            special_files[path.name] = raw
            continue

        ext_no_dot = path.suffix.lstrip(".").lower()
        assets.append(
            AssetFile(
                rel_url="/" + rel_posix,
                path=path,
                size=len(raw),
                content_type=_guess_content_type(path),
                content_hash=_compute_asset_hash(raw, ext_no_dot),
            )
        )

    if not assets:
        raise RuntimeError("No deployable assets found in dist/")
    return assets, special_files


def _load_cf_config() -> tuple[str, str, str]:
    """Load Cloudflare settings from environment variables / .env (never log values)."""
    load_dotenv()
    token = os.environ.get("CLOUDFLARE_API_TOKEN", "").strip()
    account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "").strip()
    project = os.environ.get("CLOUDFLARE_PAGES_PROJECT", DEFAULT_PROJECT_NAME).strip() or DEFAULT_PROJECT_NAME

    missing: list[str] = []
    if not token:
        missing.append("CLOUDFLARE_API_TOKEN")
    if not account_id:
        missing.append("CLOUDFLARE_ACCOUNT_ID")
    if missing:
        raise RuntimeError(
            "Missing required Cloudflare environment variable(s) in .env: "
            + ", ".join(missing)
            + ". Set a scoped API token (Cloudflare Pages: Edit) and Account ID in .env."
        )
    if len(account_id) != 32 or not all(c in "0123456789abcdefABCDEF" for c in account_id):
        raise RuntimeError(
            "CLOUDFLARE_ACCOUNT_ID format looks invalid (expected 32 hex characters)."
        )
    return token, account_id, project


def _cf_request(
    session: requests.Session,
    method: str,
    url: str,
    bearer_token: str,
    *,
    json_body: Any | None = None,
    files: dict[str, Any] | None = None,
    timeout: int = 120,
    max_retries: int = 4,
) -> dict[str, Any]:
    """Send a Cloudflare API request with retries and return the JSON payload."""
    headers = {"Authorization": f"Bearer {bearer_token}"}
    if json_body is not None:
        headers["Content-Type"] = "application/json"

    last_err: str = "unknown error"
    for attempt in range(max_retries):
        try:
            resp = session.request(
                method,
                url,
                headers=headers,
                json=json_body,
                files=files,
                timeout=timeout,
            )
        except requests.RequestException as exc:
            last_err = f"network error ({exc.__class__.__name__})"
            if attempt + 1 < max_retries:
                time.sleep(2.0 * (2**attempt))
                continue
            raise RuntimeError(f"Cloudflare API request failed: {last_err}") from None

        if resp.status_code in (429, 500, 502, 503, 504) and attempt + 1 < max_retries:
            retry_after = resp.headers.get("Retry-After")
            wait_sec = float(retry_after) if (retry_after and retry_after.isdigit()) else 2.0 * (2**attempt)
            time.sleep(min(wait_sec, 30.0))
            continue

        try:
            data = resp.json()
        except ValueError:
            raise RuntimeError(
                f"Cloudflare API returned non-JSON response (HTTP {resp.status_code})"
            ) from None

        if resp.status_code >= 400 or not data.get("success", False):
            errs = data.get("errors") or []
            err_msg = "; ".join(
                f"[{e.get('code')}] {e.get('message')}" for e in errs if isinstance(e, dict)
            ) or f"HTTP {resp.status_code}"
            raise RuntimeError(f"Cloudflare API error: {err_msg}")
        return data

    raise RuntimeError(f"Cloudflare API request failed after retries: {last_err}")


def _get_upload_jwt(
    session: requests.Session, api_token: str, account_id: str, project: str
) -> str:
    url = f"{CF_API_BASE}/accounts/{account_id}/pages/projects/{project}/upload-token"
    data = _cf_request(session, "GET", url, api_token, timeout=30)
    jwt = (data.get("result") or {}).get("jwt")
    if not jwt:
        raise RuntimeError("Cloudflare API did not return an upload JWT.")
    return str(jwt)


def _check_missing_hashes(
    session: requests.Session, jwt: str, hashes: list[str]
) -> set[str]:
    url = f"{CF_API_BASE}/pages/assets/check-missing"
    missing: set[str] = set()
    chunk_size = 5000
    for i in range(0, len(hashes), chunk_size):
        chunk = hashes[i : i + chunk_size]
        data = _cf_request(session, "POST", url, jwt, json_body={"hashes": chunk}, timeout=60)
        res = data.get("result") or []
        if isinstance(res, list):
            missing.update(str(h) for h in res)
    return missing


def _upload_missing_assets(
    session: requests.Session,
    api_token: str,
    account_id: str,
    project: str,
    jwt: str,
    missing_assets: list[AssetFile],
) -> str:
    """Upload missing assets in batches (refreshing upload JWT if it expires)."""
    url = f"{CF_API_BASE}/pages/assets/upload"
    batches: list[list[AssetFile]] = []
    cur_batch: list[AssetFile] = []
    cur_bytes = 0

    for asset in missing_assets:
        if cur_batch and (
            len(cur_batch) >= MAX_BATCH_FILES or cur_bytes + asset.size > MAX_BATCH_BYTES
        ):
            batches.append(cur_batch)
            cur_batch = []
            cur_bytes = 0
        cur_batch.append(asset)
        cur_bytes += asset.size
    if cur_batch:
        batches.append(cur_batch)

    uploaded_count = 0
    for idx, batch in enumerate(batches, start=1):
        payload = [
            {
                "key": a.content_hash,
                "value": base64.b64encode(a.path.read_bytes()).decode("ascii"),
                "metadata": {"contentType": a.content_type},
                "base64": True,
            }
            for a in batch
        ]
        try:
            _cf_request(session, "POST", url, jwt, json_body=payload, timeout=180)
        except RuntimeError as exc:
            # Refresh JWT once and retry if a long upload causes token expiration
            if "401" in str(exc) or "403" in str(exc) or "8000013" in str(exc):
                jwt = _get_upload_jwt(session, api_token, account_id, project)
                _cf_request(session, "POST", url, jwt, json_body=payload, timeout=180)
            else:
                raise
        uploaded_count += len(batch)
        print(
            f"  Uploaded batch {idx}/{len(batches)} "
            f"({uploaded_count}/{len(missing_assets)} files)",
            flush=True,
        )

    return jwt


def _upsert_hashes(session: requests.Session, jwt: str, hashes: list[str]) -> None:
    url = f"{CF_API_BASE}/pages/assets/upsert-hashes"
    chunk_size = 5000
    for i in range(0, len(hashes), chunk_size):
        chunk = hashes[i : i + chunk_size]
        _cf_request(session, "POST", url, jwt, json_body={"hashes": chunk}, timeout=60)


def _create_deployment(
    session: requests.Session,
    api_token: str,
    account_id: str,
    project: str,
    manifest: dict[str, str],
    special_files: dict[str, bytes],
) -> dict[str, Any]:
    url = f"{CF_API_BASE}/accounts/{account_id}/pages/projects/{project}/deployments"
    files: dict[str, Any] = {
        "manifest": (None, json.dumps(manifest, separators=(",", ":")), "application/json"),
    }
    for name, raw in special_files.items():
        files[name] = (name, raw, "application/octet-stream")

    data = _cf_request(session, "POST", url, api_token, files=files, timeout=120)
    return data.get("result") or {}


def deploy(dry_run: bool = False, skip_check: bool = False) -> int:
    if not skip_check:
        print("[1/5] Running integrity and security check (src.check)...", flush=True)
        rc = integrity_check.main()
        if rc != 0:
            print("Deploy aborted: src.check failed.", file=sys.stderr)
            return rc
    else:
        print("[1/5] Skipping src.check (--skip-check).", flush=True)

    print("[2/5] Scanning dist/ and computing content hashes...", flush=True)
    assets, special_files = collect_dist_files()
    total_bytes = sum(a.size for a in assets) + sum(len(b) for b in special_files.values())

    # Deduplicate by content hash to avoid uploading identical files multiple times
    unique_by_hash: dict[str, AssetFile] = {}
    manifest: dict[str, str] = {}
    for a in assets:
        manifest[a.rel_url] = a.content_hash
        unique_by_hash.setdefault(a.content_hash, a)

    print(
        f"  Prepared {len(assets)} assets ({len(unique_by_hash)} unique hashes, "
        f"{total_bytes / (1024 * 1024):.1f} MB) + config files: {sorted(special_files)}",
        flush=True,
    )

    if dry_run:
        print("DRY RUN OK: dist/ is ready for Cloudflare Pages deployment.")
        return 0

    api_token, account_id, project = _load_cf_config()
    session = requests.Session()
    session.headers["User-Agent"] = "etf-xray-deployer/1.0"

    print(f"[3/5] Requesting upload session for project '{project}'...", flush=True)
    jwt = _get_upload_jwt(session, api_token, account_id, project)

    all_hashes = list(unique_by_hash.keys())
    missing_hashes = _check_missing_hashes(session, jwt, all_hashes)
    missing_assets = [unique_by_hash[h] for h in all_hashes if h in missing_hashes]
    skipped = len(all_hashes) - len(missing_assets)
    print(
        f"[4/5] Differential check: {len(missing_assets)} to upload, "
        f"{skipped} already cached on Cloudflare",
        flush=True,
    )

    if missing_assets:
        jwt = _upload_missing_assets(
            session, api_token, account_id, project, jwt, missing_assets
        )
    _upsert_hashes(session, jwt, all_hashes)

    print("[5/5] Finalizing deployment...", flush=True)
    result = _create_deployment(
        session, api_token, account_id, project, manifest, special_files
    )
    dep_id = result.get("id", "(unknown)")
    dep_url = result.get("url", "")
    aliases = result.get("aliases") or []
    env_name = result.get("environment", "production")

    print("DEPLOY OK:")
    print(f"  - Project:     {project} ({env_name})")
    print(f"  - Deployment:  {dep_id}")
    if dep_url:
        print(f"  - Preview URL: {dep_url}")
    if aliases:
        print(f"  - Live URL:    {aliases[0]}")
    else:
        print(f"  - Live URL:    https://{project}.pages.dev")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Deploy dist/ to Cloudflare Pages using Direct Upload API."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run check.py and scan dist/ without contacting Cloudflare.",
    )
    parser.add_argument(
        "--skip-check",
        action="store_true",
        help="Skip src.check before deploying (not recommended).",
    )
    args = parser.parse_args()
    try:
        return deploy(dry_run=args.dry_run, skip_check=args.skip_check)
    except RuntimeError as exc:
        print(f"DEPLOY FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
