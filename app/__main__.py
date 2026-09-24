"""python -m app：启动本地 HTTP 服务。"""
from __future__ import annotations

import argparse
import os

from .web import create_server


def main() -> None:
    parser = argparse.ArgumentParser(description="防护物资/演练设备预约服务（本地模拟）")
    parser.add_argument("--db", default=os.environ.get("APP_DB", "data/app.db"))
    parser.add_argument("--host", default=os.environ.get("APP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("APP_PORT", "8080")))
    args = parser.parse_args()

    httpd = create_server(args.db, args.host, args.port)
    print(f"服务已启动: http://{args.host}:{args.port}  (db={args.db})")
    print("OpenAPI 文档: GET /api/v1/openapi")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


if __name__ == "__main__":
    main()
