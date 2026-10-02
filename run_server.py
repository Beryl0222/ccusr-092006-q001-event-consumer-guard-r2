#!/usr/bin/env python3
"""免安装启动脚本：python3 run_server.py [--port 8080] [--db data/guard.db]"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from guard.server import serve  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="赛事消费异常联防后端（独立运行）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--db", default="data/guard.db")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    httpd = serve(args.host, args.port, args.db, quiet=args.quiet)
    print(f"赛事消费异常联防后端已启动: http://{args.host}:{args.port}  (db={args.db})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
