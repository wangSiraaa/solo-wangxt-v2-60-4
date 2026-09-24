"""HTTP API（标准库 http.server，零三方依赖）。

每个请求使用独立的 SQLite 连接（线程隔离）；写操作统一走
BEGIN IMMEDIATE + 数据库触发器，保证并发下不超卖。
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .db import init_db
from .errors import ApiError, dumps
from .service import Service

ROUTES = [
    ("GET",    r"/api/v1/health$",                       "health"),
    ("GET",    r"/api/v1/openapi$",                      "openapi"),
    ("GET",    r"/api/v1/sections$",                     "sections"),
    ("GET",    r"/api/v1/resources$",                    "resources"),
    ("POST",   r"/api/v1/resources$",                    "create_resource"),
    ("GET",    r"/api/v1/resources/(?P<id>[A-Za-z0-9_\-]+)$", "resource"),
    ("POST",   r"/api/v1/resources/(?P<id>[A-Za-z0-9_\-]+)/offline$", "resource_offline"),
    ("POST",   r"/api/v1/resources/(?P<id>[A-Za-z0-9_\-]+)/online$",  "resource_online"),
    ("GET",    r"/api/v1/resource-units$",               "units"),
    ("POST",   r"/api/v1/resource-units/(?P<id>\d+)/maintenance$", "unit_maintenance"),
    ("POST",   r"/api/v1/resource-units/(?P<id>\d+)/available$",   "unit_available"),
    ("GET",    r"/api/v1/plans$",                        "plans"),
    ("POST",   r"/api/v1/plans$",                        "create_plan"),
    ("GET",    r"/api/v1/plans/(?P<id>\d+)$",            "plan"),
    ("POST",   r"/api/v1/plans/(?P<id>\d+)/reservations$", "reserve"),
    ("GET",    r"/api/v1/plans/(?P<id>\d+)/readiness$",  "readiness"),
    ("POST",   r"/api/v1/plans/(?P<id>\d+)/start$",      "start"),
    ("POST",   r"/api/v1/plans/(?P<id>\d+)/release$",    "release"),
    ("POST",   r"/api/v1/plans/(?P<id>\d+)/replacements$", "replace"),
    ("GET",    r"/api/v1/audit$",                        "audit"),
]


def build_handler(db_path: str, spec: dict):
    class Handler(BaseHTTPRequestHandler):
        server_version = "DrillReservation/1.0"

        def log_message(self, fmt, *args):  # 精简日志
            pass

        def _send(self, code: int, payload, extra_headers=None):
            data = dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            for k, v in (extra_headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _read_body(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            if not raw:
                return {}, ""
            try:
                return json.loads(raw.decode("utf-8")), raw.decode("utf-8")
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ApiError("INVALID_JSON", f"请求体不是合法 JSON: {exc}", 400)

        def _match(self, method: str):
            path = self.path.split("?", 1)[0]
            for m, pattern, action in ROUTES:
                if m != method:
                    continue
                match = re.fullmatch(pattern, path)
                if match:
                    return action, match.groupdict()
            return None, None

        def _query(self) -> dict:
            from urllib.parse import parse_qs
            query = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            return {k: v[-1] for k, v in query.items()}

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def _dispatch(self, method: str):
            action, kwargs = self._match(method)
            if action is None:
                self._send(404, {"error": {"code": "NOT_FOUND", "message": "未知接口"}})
                return
            conn = init_db(self.server.db_path, seed=True)  # 每请求独立连接（线程隔离）
            try:
                self._route(action, kwargs, Service(conn))
            except ApiError as exc:
                self._send(exc.http_status, exc.body())
            except Exception as exc:  # noqa: BLE001
                self._send(500, {"error": {"code": "INTERNAL_ERROR", "message": str(exc)}})
            finally:
                conn.close()

        def _route(self, action: str, kwargs: dict, svc: Service):
            body, raw = (None, "")
            if self.command == "POST":
                body, raw = self._read_body()
            headers = {}
            headers = {}

            if action == "health":
                self._send(200, {"status": "ok"})
            elif action == "openapi":
                self._send(200, self.server.spec)
            elif action == "sections":
                from .sections import SECTIONS, MUTEX_MATRIX
                self._send(200, {"sections": list(SECTIONS), "mutex_matrix": MUTEX_MATRIX})
            elif action == "resources":
                self._send(200, {"resources": svc.store.list_resources()})
            elif action == "create_resource":
                from .errors import require
                code = require(body, "code", str)
                name = require(body, "name", str)
                kind = require(body, "kind", str)
                quantity = require(body, "quantity", int)
                if kind not in ("MATERIAL", "EQUIPMENT"):
                    raise ApiError("VALIDATION_ERROR", "kind 必须为 MATERIAL/EQUIPMENT", 400)
                if quantity <= 0:
                    raise ApiError("VALIDATION_ERROR", "quantity 必须为正整数", 400)
                from .timeutil import now_iso
                from .db import transaction
                ts = now_iso()
                with transaction(svc.conn):
                    svc.store.create_resource(code, name, kind, quantity, ts)
                    svc.store.insert_audit("RESOURCE_CREATED", "resource", code,
                                           {"name": name, "kind": kind,
                                            "quantity": quantity}, ts)
                self._send(201, {"resource": svc.store.get_resource(code)})
            elif action == "resource":
                self._send(200, {"resource": svc.store.get_resource(kwargs["id"])})
            elif action == "resource_offline":
                self._send(200, {"resource": svc.set_resource_online(kwargs["id"], False)})
            elif action == "resource_online":
                self._send(200, {"resource": svc.set_resource_online(kwargs["id"], True)})
            elif action == "units":
                q = self._query()
                self._send(200, {"units": svc.store.list_units(q.get("resource_code"))})
            elif action == "unit_maintenance":
                self._send(200, {"unit": svc.set_unit_maintenance(int(kwargs["id"]), True)})
            elif action == "unit_available":
                self._send(200, {"unit": svc.set_unit_maintenance(int(kwargs["id"]), False)})
            elif action == "plans":
                self._send(200, {"plans": svc.store.list_plans()})
            elif action == "create_plan":
                self._send(201, {"plan": svc.create_plan(body)})
            elif action == "plan":
                self._send(200, {"plan": svc.get_plan_detail(int(kwargs["id"]))})
            elif action == "reserve":
                idem = self._idempotency(method="POST", raw=raw)
                result, code, replayed = svc.reserve(int(kwargs["id"]), body, idem)
                if replayed:
                    headers["Idempotency-Replayed"] = "true"
                self._send(code, result, headers)
            elif action == "readiness":
                self._send(200, svc.readiness(int(kwargs["id"])))
            elif action == "start":
                self._send(200, svc.start(int(kwargs["id"])))
            elif action == "release":
                self._send(200, svc.release(int(kwargs["id"])))
            elif action == "replace":
                self._send(200, svc.replace_unit(int(kwargs["id"]), body or {}))
            elif action == "audit":
                q = self._query()
                limit = min(int(q.get("limit", 100)), 500)
                offset = int(q.get("offset", 0))
                rows = svc.store.list_audit(limit, offset,
                                            q.get("entity_type"), q.get("entity_id"))
                self._send(200, {"audit": rows, "limit": limit, "offset": offset})

        def _idempotency(self, method: str, raw: str):
            key = self.headers.get("Idempotency-Key")
            if key:
                path = self.path.split("?", 1)[0]
                return key, method, path, raw
            return None

    Handler.db_path = db_path
    return Handler


def create_server(db_path: str, host: str = "127.0.0.1", port: int = 8080,
                  spec: dict | None = None) -> ThreadingHTTPServer:
    from .openapi import OPENAPI
    handler_cls = build_handler(db_path, spec or OPENAPI)
    # 初始化（含种子）
    init_db(db_path, seed=True).close()
    httpd = ThreadingHTTPServer((host, port), handler_cls)
    httpd.db_path = db_path
    httpd.spec = spec or OPENAPI
    return httpd
