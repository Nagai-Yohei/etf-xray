"""Environment variable loader and shared project path definitions.

Security principles:
- Never print, log, or copy the contents of .env.
- Never include raw SEC_USER_AGENT values in exception messages.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
DATA_DIR = PROJECT_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
DB_PATH = DATA_DIR / "market.db"
WEB_DIR = PROJECT_ROOT / "web"
DIST_DIR = PROJECT_ROOT / "dist"
ENV_PATH = PROJECT_ROOT / ".env"

_DUMMY_UA_PATTERN = re.compile(
    r"@example\.(com|org|net)|your-name|changeme|dummy",
    re.IGNORECASE,
)


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1].strip()
    return value


def parse_dotenv_file(path: Path = ENV_PATH) -> dict[str, str]:
    """Load the local .env file into a dict (never emit externally)."""
    result: dict[str, str] = {}
    if not path.is_file():
        return result
    with path.open("r", encoding="utf-8") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            if key:
                result[key] = _strip_quotes(val)
    return result


def load_dotenv(path: Path = ENV_PATH) -> None:
    """Populate unset environment variables from .env."""
    for key, val in parse_dotenv_file(path).items():
        os.environ.setdefault(key, val)


def get_sec_user_agent() -> str:
    """Return the SEC EDGAR User-Agent; fail fast if unset or a placeholder."""
    load_dotenv()
    ua = os.environ.get("SEC_USER_AGENT", "").strip()
    if not ua or len(ua) < 6 or _DUMMY_UA_PATTERN.search(ua):
        raise RuntimeError(
            "SEC_USER_AGENT is not configured with a valid value. "
            "Set SEC_USER_AGENT in .env or environment variables before accessing SEC EDGAR."
        )
    return ua


def get_openfigi_api_key() -> str | None:
    """Return the OpenFIGI API key if configured, or None."""
    load_dotenv()
    key = os.environ.get("OPENFIGI_API_KEY", "").strip()
    return key or None
