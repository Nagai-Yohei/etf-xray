#!/usr/bin/env bash
# etf-xray CLI helper script

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON_BIN="$ROOT_DIR/.venv/bin/python"

if [ ! -x "$PYTHON_BIN" ]; then
  echo "Error: .venv not found. Create a virtual environment with Python 3.10+ first." >&2
  exit 1
fi

CMD="${1:-help}"
shift || true

case "$CMD" in
  build)
    exec "$PYTHON_BIN" -m src.pipeline "$@"
    ;;
  export)
    exec "$PYTHON_BIN" -m src.export_site "$@"
    ;;
  check)
    exec "$PYTHON_BIN" -m src.check "$@"
    ;;
  test)
    exec "$PYTHON_BIN" -m unittest discover -s "$ROOT_DIR/tests" -v "$@"
    ;;
  init-db)
    exec "$PYTHON_BIN" -c "from src.db import init_db; init_db().close(); print('DB initialized.')"
    ;;
  serve)
    exec "$PYTHON_BIN" -m src.serve "$@"
    ;;
  deploy)
    exec "$PYTHON_BIN" -m src.deploy "$@"
    ;;
  *)
    cat <<'EOF'
Usage: ./run.sh <command>

Commands:
  build      Run full pipeline (fetch SEC filings -> XBRL -> metrics -> export dist/)
  export     Re-export dist/ (web assets + split JSON) from existing SQLite DB
  check      Run data integrity checks + secret/PII leak scan + unit tests
  test       Run unit test suite (tests/)
  init-db    Initialize local SQLite database (data/market.db)
  serve      Start local HTTP server with production CSP headers
  deploy     Upload dist/ to Cloudflare Pages via Direct Upload API
EOF
    ;;
esac
