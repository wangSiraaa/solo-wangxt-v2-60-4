"""集成测试基类：临时文件数据库 + 真实 HTTP 服务（线程模式）。

使用真实网络栈（http.server 线程池 + urllib 多线程请求），
覆盖“并发 HTTP 请求绝不超卖”这一关键验收项。
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.client import RemoteDisconnected

from app.web import create_server


class HttpIntegrationTest(unittest.TestCase):
    HOST = "127.0.0.1"
    RESET_DB_PER_CLASS = True   # 每个测试类使用干净库存；重启一致性类关闭此开关

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.db_path = os.path.join(cls.tmpdir.name, "it.db")
        cls.httpd = create_server(cls.db_path, cls.HOST, 0)
        cls._port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)
        cls.tmpdir.cleanup()

    @property
    def port(self) -> int:
        # 重启一致性测试会换端口，因此始终动态读取
        return self.httpd.server_address[1]

    @property
    def base(self) -> str:
        return f"http://{self.HOST}:{self.port}"

    def shutdown_server(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    def reopen_server(self):
        """模拟进程重启：关闭并以同一数据库文件重新打开（不删除 WAL，等价真实重启）。"""
        self.shutdown_server()
        self.httpd = create_server(self.db_path, self.HOST, 0)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def reset_database(self):
        """清空并重新播种（测试隔离）：先做 WAL 检查点，再安全删除全部数据库文件。"""
        import sqlite3
        self.shutdown_server()
        # 确保 WAL 内容并入主库文件后再删除，避免丢数据假象
        guard = sqlite3.connect(self.db_path)
        guard.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        guard.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self.db_path + suffix)
            except FileNotFoundError:
                pass
        self.httpd = create_server(self.db_path, self.HOST, 0)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def setUp(self):
        # 每个测试方法前重置（首个方法无需重置；用标记位判断）
        if self.RESET_DB_PER_CLASS:
            self.reset_database()

    def request(self, method: str, path: str, body=None, headers=None,
                timeout: float = 30.0, raw_body: str | None = None):
        """返回 (status, headers, body)。"""
        if raw_body is not None:
            data = raw_body.encode("utf-8")
        else:
            data = json.dumps(body).encode("utf-8") if body is not None else None
        hdrs = {"Content-Type": "application/json"}
        if headers:
            hdrs.update(headers)
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, dict(resp.headers), json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), json.loads(exc.read().decode("utf-8"))
        except (urllib.error.URLError, RemoteDisconnected) as exc:
            self.fail(f"请求失败 {method} {path}: {exc}")

    # ---------- 业务便捷方法 ----------
    def create_plan(self, section="SECTION_Z1", start="2026-10-10T00:00:00Z",
                    end="2026-10-10T02:00:00Z", name=None) -> dict:
        status, _, body = self.request("POST", "/api/v1/plans", {
            "name": name or f"plan-{section}", "section": section,
            "window_start": start, "window_end": end,
        })
        self.assertEqual(status, 201, body)
        return body["plan"]

    def reserve(self, plan_id: int, items=None, key=None, raw_body: str | None = None,
                timeout: float = 30.0):
        headers = {"Idempotency-Key": key} if key else None
        return self.request("POST", f"/api/v1/plans/{plan_id}/reservations",
                            body=items, headers=headers, timeout=timeout, raw_body=raw_body)

    def get_resource(self, code: str) -> dict:
        status, _, body = self.request("GET", f"/api/v1/resources/{code}")
        self.assertEqual(status, 200, body)
        return body["resource"]

    def db_query_all(self, sql: str, args=()):
        """测试专用：绕过 API 直接核验持久化状态（触发后重启一致性等）。"""
        import sqlite3
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]
        finally:
            conn.close()

    def list_units(self, code: str) -> list[dict]:
        status, _, body = self.request("GET", f"/api/v1/resource-units?resource_code={code}")
        self.assertEqual(status, 200, body)
        return body["units"]

    def assert_error(self, status, body, code):
        self.assertEqual(status, 409 if code != "NOT_FOUND" else 404)
        self.assertEqual(body["error"]["code"], code)
