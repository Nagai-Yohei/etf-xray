"""ローカル確認用静的 HTTP サーバー（dist/ を配信）。

web/_headers のセキュリティヘッダ（厳格 CSP 等）をローカルでも同様に付与し、
本番 Cloudflare Pages 環境と同じ制約下で動作確認できるようにする。
"""

from __future__ import annotations

import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from src.env import DIST_DIR, WEB_DIR


def _load_security_headers() -> list[tuple[str, str]]:
    headers_file = DIST_DIR / "_headers"
    if not headers_file.is_file():
        headers_file = WEB_DIR / "_headers"
    if not headers_file.is_file():
        return []

    parsed: list[tuple[str, str]] = []
    for raw_line in headers_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("/"):
            continue
        if ":" in line:
            key, val = line.split(":", 1)
            parsed.append((key.strip(), val.strip()))
    return parsed


class SecureStaticHandler(SimpleHTTPRequestHandler):
    """CSP・セキュリティヘッダ付き静的ファイルハンドラ。"""

    security_headers: list[tuple[str, str]] = []

    def end_headers(self) -> None:
        for key, val in self.security_headers:
            self.send_header(key, val)
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve dist/ locally with production CSP headers")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8787, help="Bind port (default: 8787)")
    args = parser.parse_args()

    if not DIST_DIR.is_dir():
        raise SystemExit("dist/ does not exist. Run './run.sh export' first.")

    SecureStaticHandler.security_headers = _load_security_headers()
    handler = partial(SecureStaticHandler, directory=str(DIST_DIR))
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"Serving {DIST_DIR.name}/ at http://{args.host}:{args.port}/ (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
