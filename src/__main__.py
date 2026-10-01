"""独立运行入口：python -m src.ecg  (等价 python -m src)

环境变量：
  ECG_HTTP_HOST   默认 127.0.0.1
  ECG_HTTP_PORT   默认 8080
  ECG_DB_PATH     默认 data/guard.db（首次启动自动建库与种子数据）
"""

from __future__ import annotations

import argparse
import os

from .api import make_server


def main() -> None:
    parser = argparse.ArgumentParser(description="赛事消费异常联防后端")
    parser.add_argument("--host", default=os.environ.get("ECG_HTTP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("ECG_HTTP_PORT", "8080")))
    parser.add_argument("--db", default=os.environ.get("ECG_DB_PATH", "data/guard.db"))
    parser.add_argument("--verbose", action="store_true", help="打印 HTTP 访问日志")
    args = parser.parse_args()

    httpd = make_server(args.host, args.port, args.db, verbose=args.verbose)
    print(f"赛事消费异常联防后端已启动: http://{args.host}:{args.port}  数据库: {args.db}")
    print("健康检查: GET /healthz  （其余接口见 README，需 Bearer 令牌）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭…")
    finally:
        httpd.shutdown()
        httpd.db.close()


if __name__ == "__main__":
    main()
