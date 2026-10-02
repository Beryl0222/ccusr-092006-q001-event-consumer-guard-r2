"""``python -m guard`` 启动入口。

用法：
    python -m guard --host 0.0.0.0 --port 8080 --db ./guard.db
默认监听 127.0.0.0:8080，使用文件 SQLite（./data/guard.db）。
"""

from __future__ import annotations

import argparse

from .server import serve


def main() -> None:
    parser = argparse.ArgumentParser(description="赛事消费异常联防后端")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--db", default="data/guard.db", help="SQLite 路径，:memory: 为纯内存")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    httpd = serve(args.host, args.port, args.db, quiet=args.quiet)
    if not args.quiet:
        print(f"赛事消费异常联防后端已启动: http://{args.host}:{args.port}  (db={args.db})")
        print("按 Ctrl+C 停止")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭…")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
