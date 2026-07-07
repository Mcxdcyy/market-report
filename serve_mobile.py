#!/usr/bin/env python3
"""局域网静态服务：手机与电脑同一 WiFi 下访问复盘报表。

用法:
  python3 serve_mobile.py          # 默认 8765 端口
  python3 serve_mobile.py 9000     # 指定端口

手机浏览器打开终端显示的 http://<本机IP>:端口/ 即可。
"""

from __future__ import annotations

import http.server
import socket
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
DEFAULT_PORT = 8765


def local_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    ip = local_ip()
    handler = lambda *args, **kwargs: http.server.SimpleHTTPRequestHandler(  # noqa: E731
        *args, directory=str(BASE), **kwargs
    )
    server = http.server.ThreadingHTTPServer(("0.0.0.0", port), handler)
    print("=" * 48)
    print("  增长2026 · 复盘报表 · 手机访问")
    print("=" * 48)
    print(f"  手机浏览器:  http://{ip}:{port}/")
    print(f"  电脑本地:    http://127.0.0.1:{port}/")
    print(f"  目录:        {BASE}")
    print("  按 Ctrl+C 停止服务")
    print("=" * 48)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
