"""Production web entrypoint for VPS/container deployment."""
from __future__ import annotations

import os

from waitress import serve

from web_app import app


def main():
    host = str(os.environ.get("WEB_HOST") or "127.0.0.1").strip()
    port = int(os.environ.get("WEB_PORT") or 5000)
    threads = max(4, min(int(os.environ.get("WEB_THREADS") or 16), 64))
    admin_password = str(os.environ.get("WEB_ADMIN_PASSWORD") or "")

    if host not in {"127.0.0.1", "localhost", "::1"}:
        if len(admin_password) < 16 or admin_password.startswith("replace-with-"):
            raise SystemExit("拒绝启动公网管理面：WEB_ADMIN_PASSWORD 必须是至少 16 位的非默认强密码")

    print(f"[*] Production web server listening on {host}:{port} (threads={threads})")
    serve(
        app,
        host=host,
        port=port,
        threads=threads,
        channel_timeout=300,
        cleanup_interval=30,
    )


if __name__ == "__main__":
    main()
