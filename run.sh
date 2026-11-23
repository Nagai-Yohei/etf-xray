#!/usr/bin/env bash
# etf-xray 実行ヘルパー

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON_BIN="$ROOT_DIR/.venv/bin/python"

if [ ! -x "$PYTHON_BIN" ]; then
  echo "エラー: .venv が見つかりません。Python 3.10+ で .venv を作成してください。" >&2
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
  init-db)
    exec "$PYTHON_BIN" -c "from src.db import init_db; init_db().close(); print('DB initialized.')"
    ;;
  serve)
    exec "$PYTHON_BIN" -m src.serve "$@"
    ;;
  *)
    cat <<'EOF'
Usage: ./run.sh <command>

Commands:
  build      全データパイプラインを実行（ETF 取得 → XBRL → 指標計算 → dist/data 出力）
  export     SQLite から dist/data のみ再出力
  check      データ整合性検査 ＋ セキュリティ・情報流出スキャンを実行
  init-db    SQLite データベース（data/market.db）を初期化
  serve      ローカル確認用 HTTP サーバーを起動
EOF
    ;;
esac
