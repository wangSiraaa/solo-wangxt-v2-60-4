"""验收：持久化约束、重启后库存/预约/历史一致、OpenAPI、审计只追加。"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import unittest

from app.db import init_db
from app.web import create_server
from tests.base import HttpIntegrationTest

W_S = "2026-10-30T00:00:00Z"
W_E = "2026-10-30T03:00:00Z"


class RestartConsistencyTest(HttpIntegrationTest):
    def test_restart_preserves_inventory_reservations_and_history(self):
        # 通过 API 造数据：预约 + 维修 + 阻断尝试 + 销记
        p1 = self.create_plan(section="SECTION_Z1", start=W_S, end=W_E)
        s, _, b1 = self.reserve(p1["id"], {"items": [
            {"resource_code": "OXYGEN_KIT", "quantity": 2},
            {"resource_code": "PROTECTIVE_SUIT", "quantity": 3},
        ]}, key="persist-key-1", raw_body=json.dumps({"items": [
            {"resource_code": "OXYGEN_KIT", "quantity": 2},
            {"resource_code": "PROTECTIVE_SUIT", "quantity": 3}]}))
        self.assertEqual(s, 201, b1)

        p2 = self.create_plan(section="SECTION_Z2", start=W_S, end=W_E)
        s, _, _ = self.reserve(p2["id"], {"items": [
            {"resource_code": "OXYGEN_KIT", "quantity": 2}]})
        self.assertEqual(s, 409)  # 仅剩 1 台

        units = b1["reservation"]["lines"]
        oxygen_line = next(l for l in units if l["resource_code"] == "OXYGEN_KIT")
        self.request("POST",
                     f"/api/v1/resource-units/{oxygen_line['unit_ids'][0]}/maintenance", {})
        s, _, _ = self.request("POST", f"/api/v1/plans/{p1['id']}/start", {})
        self.assertEqual(s, 409)
        self.request("POST",
                     f"/api/v1/resource-units/{oxygen_line['unit_ids'][0]}/available", {})
        self.request("POST", f"/api/v1/plans/{p1['id']}/start", {})
        self.request("POST", f"/api/v1/plans/{p1['id']}/release", {})

        # 重启前快照
        before = {
            "audit": self.db_query_all("SELECT action,status,entity_type,entity_id,plan_id,detail_json FROM audit_log ORDER BY id"),
            "oxygen_alloc_held": self.db_query_all(
                """SELECT a.unit_id,a.window_start,a.window_end FROM allocations a
                   JOIN reservation_lines l ON l.id=a.line_id
                   WHERE a.resource_code='OXYGEN_KIT' AND l.status='RELEASED' ORDER BY a.unit_id"""),
            "request": self.db_query_all("SELECT plan_id,status,released_at FROM reservation_requests"),
            "idemp": self.db_query_all("SELECT idempotency_key,response_code FROM idempotency_keys"),
        }

        # 重启服务：以同一数据库文件重新打开（等价真实进程重启）
        self.reopen_server()

        # 库存一致
        r = self.get_resource("OXYGEN_KIT")
        self.assertEqual(r["total_units"], 3)
        self.assertEqual(r["maintenance_units"], 0)
        self.assertEqual(r["occupied_units"], 0)
        self.assertEqual(r["available_now"], 3)

        # 预约历史一致：p1 已 RELEASED，p2 没有预约单
        s, _, detail = self.request("GET", f"/api/v1/plans/{p1['id']}")
        self.assertEqual(s, 200)
        self.assertEqual(detail["plan"]["status"], "COMPLETED")
        self.assertEqual(detail["plan"]["reservation"]["status"], "RELEASED")
        self.assertTrue(all(l["status"] == "RELEASED"
                            for l in detail["plan"]["reservation"]["lines"]))
        s, _, detail2 = self.request("GET", f"/api/v1/plans/{p2['id']}")
        self.assertIsNone(detail2["plan"]["reservation"])

        # 审计历史逐行一致
        after_audit = self.db_query_all(
            "SELECT action,status,entity_type,entity_id,plan_id,detail_json FROM audit_log ORDER BY id")
        self.assertEqual(after_audit, before["audit"])
        actions = {a["action"] for a in after_audit}
        for required in ("PLAN_CREATED", "RESERVED", "RESERVE_DENIED",
                         "START_BLOCKED", "PLAN_STARTED", "RELEASED",
                         "UNIT_MAINTENANCE", "UNIT_AVAILABLE"):
            self.assertIn(required, actions, required)

        # 幂等表一致（重放仍命中首次结果）
        raw = json.dumps({"items": [
            {"resource_code": "OXYGEN_KIT", "quantity": 2},
            {"resource_code": "PROTECTIVE_SUIT", "quantity": 3}]})
        s, _, body = self.request("POST", f"/api/v1/plans/{p1['id']}/reservations",
                                  None, headers={"Idempotency-Key": "persist-key-1"},
                                  raw_body=raw)
        self.assertEqual(s, 201)
        self.assertTrue(body["replayed"])

        # 释放后窗口可再次被新计划复用（库存真正回来）
        p3 = self.create_plan(section="SECTION_Z3", start=W_S, end=W_E)
        s, _, body = self.reserve(p3["id"], {"items": [
            {"resource_code": "OXYGEN_KIT", "quantity": 3}]})
        self.assertEqual(s, 201, body)


class DatabaseConstraintTest(HttpIntegrationTest):
    """即使绕过应用层直接写库，触发器仍拒绝超卖/失效占用，审计不可篡改。"""

    def _raw(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def test_trigger_blocks_overlap_at_database_level(self):
        p = self.create_plan(section="SECTION_Z1", start=W_S, end=W_E)
        s, _, body = self.reserve(p["id"], {"items": [
            {"resource_code": "OXYGEN_KIT", "quantity": 1}]})
        self.assertEqual(s, 201)
        uid = body["reservation"]["lines"][0]["unit_ids"][0]
        request_id = body["reservation"]["request_id"]

        conn = self._raw()
        try:
            # 合法外键 + 重叠窗口：重叠触发器必须拒绝
            conn.execute("BEGIN IMMEDIATE")
            lid2 = conn.execute(
                """INSERT INTO reservation_lines
                   (request_id,resource_code,quantity,window_start,window_end,created_at)
                   VALUES (?,?,1,?,?,?)""",
                (request_id, "MASK_N95", W_S, W_E, W_S)).lastrowid
            # 本行是 MASK_N95，但尝试占用 OXYGEN 实例 uid（跨资源盗用同一实例）
            with self.assertRaises(sqlite3.IntegrityError) as ctx:
                conn.execute(
                    """INSERT INTO allocations(line_id,unit_id,resource_code,window_start,window_end)
                       VALUES (?,?,?,?,?)""",
                    (lid2, uid, "OXYGEN_KIT", W_S, W_E))
            self.assertIn("ALLOC_OVERLAP", str(ctx.exception))
            conn.execute("ROLLBACK")

            # 首尾相接：触发器放行（实例在 t 点可复用）
            conn.execute("BEGIN IMMEDIATE")
            lid3 = conn.execute(
                """INSERT INTO reservation_lines
                   (request_id,resource_code,quantity,window_start,window_end,created_at)
                   VALUES (?,?,1,?,?,?)""",
                (request_id, "MASK_N95",
                 "2026-10-30T03:00:00Z", "2026-10-30T04:00:00Z", W_S)).lastrowid
            conn.execute(
                """INSERT INTO allocations(line_id,unit_id,resource_code,window_start,window_end)
                   VALUES (?,?,?,?,?)""",
                (lid3, uid, "OXYGEN_KIT",
                 "2026-10-30T03:00:00Z", "2026-10-30T04:00:00Z"))
            conn.execute("COMMIT")
        finally:
            conn.close()

    def test_trigger_blocks_allocation_to_maintenance_unit(self):
        units = self.list_units("GENSET_5KW")
        uid = units[0]["id"]
        self.request("POST", f"/api/v1/resource-units/{uid}/maintenance", {})

        conn = self._raw()
        try:
            conn.execute("BEGIN IMMEDIATE")
            plan_id = conn.execute(
                "INSERT INTO plans(name,section,window_start,window_end,created_at) "
                "VALUES ('x','SECTION_Z1',?,?,?)",
                (W_S, W_E, W_S)).lastrowid
            req_id = conn.execute(
                "INSERT INTO reservation_requests(plan_id,created_at) VALUES (?,?)",
                (plan_id, W_S)).lastrowid
            lid = conn.execute(
                """INSERT INTO reservation_lines
                   (request_id,resource_code,quantity,window_start,window_end,created_at)
                   VALUES (?,?,1,?,?,?)""",
                (req_id, "GENSET_5KW", W_S, W_E, W_S)).lastrowid
            with self.assertRaises(sqlite3.IntegrityError) as ctx:
                conn.execute(
                    """INSERT INTO allocations(line_id,unit_id,resource_code,window_start,window_end)
                       VALUES (?,?,?,?,?)""",
                    (lid, uid, "GENSET_5KW", W_S, W_E))
            self.assertIn("UNIT_NOT_AVAILABLE", str(ctx.exception))
            conn.execute("ROLLBACK")
        finally:
            conn.close()

    def test_audit_log_is_append_only(self):
        p = self.create_plan(section="SECTION_Z1", start=W_S, end=W_E)
        # PLAN_CREATED 以 plan 实体、id=计划号记录
        existing = self.db_query_all(
            "SELECT id,action FROM audit_log WHERE entity_type='plan' AND entity_id=?",
            (p["id"],))
        self.assertTrue(existing)
        row_id = existing[0]["id"]
        conn = self._raw()
        try:
            # UPDATE 命中真实存在的行：BEFORE UPDATE 触发器逐行触发并拒绝
            with self.assertRaises(sqlite3.IntegrityError) as ctx:
                conn.execute("UPDATE audit_log SET action='HACKED' WHERE id=?", (row_id,))
            self.assertIn("AUDIT_IMMUTABLE", str(ctx.exception))
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("DELETE FROM audit_log WHERE id=?", (row_id,))
        finally:
            conn.close()
        # 行内容未被篡改
        after = self.db_query_all("SELECT action FROM audit_log WHERE id=?", (row_id,))
        self.assertEqual(after[0]["action"], existing[0]["action"])


class OpenApiTest(HttpIntegrationTest):
    def test_openapi_served_and_matches_routes(self):
        s, _, spec = self.request("GET", "/api/v1/openapi")
        self.assertEqual(s, 200)
        self.assertEqual(spec["openapi"], "3.0.3")
        # 关键能力全部在规范中声明
        for path in ("/api/v1/plans/{id}/reservations",
                     "/api/v1/plans/{id}/readiness",
                     "/api/v1/plans/{id}/start",
                     "/api/v1/plans/{id}/release",
                     "/api/v1/plans/{id}/replacements",
                     "/api/v1/resources", "/api/v1/audit"):
            self.assertIn(path, spec["paths"], path)
        # 幂等参数在预约路径上声明（路径级 parameters）
        params = spec["paths"]["/api/v1/plans/{id}/reservations"]["parameters"]
        self.assertTrue(any(p.get("name") == "Idempotency-Key" for p in params))

    def test_audit_endpoint_filters_and_paginates(self):
        p = self.create_plan(section="SECTION_Z1", start=W_S, end=W_E)
        s, _, body = self.request(
            "GET", f"/api/v1/audit?entity_type=plan&entity_id={p['id']}&limit=1")
        self.assertEqual(s, 200)
        self.assertLessEqual(len(body["audit"]), 1)
        if body["audit"]:
            self.assertEqual(body["audit"][0]["entity_id"], str(p["id"]))


class ColdBootFromFileTest(unittest.TestCase):
    """全新数据库文件冷启动：建表 + 种子幂等可重复执行。"""

    def test_cold_boot_seeds_resources(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "cold.db")
            conn = init_db(path, seed=True)
            n = conn.execute("SELECT COUNT(*) n FROM resources").fetchone()["n"]
            self.assertEqual(n, 5)
            units = conn.execute("SELECT COUNT(*) n FROM resource_units").fetchone()["n"]
            self.assertEqual(units, 25)
            conn.close()
            # 再次初始化不重复播种
            conn = init_db(path, seed=True)
            self.assertEqual(conn.execute("SELECT COUNT(*) n FROM resources").fetchone()["n"], 5)
            conn.close()

