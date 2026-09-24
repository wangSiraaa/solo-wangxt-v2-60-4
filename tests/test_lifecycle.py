"""验收：资源状态、开工前失效逐项阻断、替换恢复、销记仅一次、审计。"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

from tests.base import HttpIntegrationTest

W_S = "2026-10-20T00:00:00Z"
W_E = "2026-10-20T02:00:00Z"


class ResourceStateTest(HttpIntegrationTest):
    def test_unit_lifecycle_available_maintenance_occupied(self):
        units = self.list_units("RADIO_SET")
        uid = units[0]["id"]
        self.assertEqual(units[0]["status"], "AVAILABLE")

        s, _, body = self.request("POST", f"/api/v1/resource-units/{uid}/maintenance", {})
        self.assertEqual(s, 200, body)
        self.assertEqual(body["unit"]["status"], "MAINTENANCE")

        s, _, body = self.request("POST", f"/api/v1/resource-units/{uid}/available", {})
        self.assertEqual(s, 200)
        self.assertEqual(body["unit"]["status"], "AVAILABLE")

    def test_resource_offline_blocks_new_booking(self):
        s, _, _ = self.request("POST", "/api/v1/resources/RADIO_SET/offline", {})
        self.assertEqual(s, 200)
        r = self.get_resource("RADIO_SET")
        self.assertFalse(r["online"])
        self.assertEqual(r["status"], "MAINTENANCE")
        self.assertEqual(r["available_now"], 0)

        p = self.create_plan(section="SECTION_Z1", start=W_S, end=W_E)
        s, _, body = self.reserve(p["id"], {"items": [
            {"resource_code": "RADIO_SET", "quantity": 1}]})
        self.assertEqual(s, 409)
        self.assertEqual(body["error"]["code"], "RESOURCE_OFFLINE")
        # 恢复
        s, _, _ = self.request("POST", "/api/v1/resources/RADIO_SET/online", {})
        self.assertEqual(s, 200)

    def test_maintenance_unit_not_bookable(self):
        units = self.list_units("RADIO_SET")
        uid = units[0]["id"]
        s, _, _ = self.request("POST", f"/api/v1/resource-units/{uid}/maintenance", {})
        self.assertEqual(s, 200)
        p = self.create_plan(section="SECTION_Z1", start=W_S, end=W_E)
        # 4 台中 1 台维修，订 4 台失败，订 3 台成功
        s4, _, b4 = self.reserve(p["id"], {"items": [
            {"resource_code": "RADIO_SET", "quantity": 4}]})
        self.assertEqual(s4, 409)
        self.assertEqual(b4["error"]["details"][0]["available"], 3)
        self.request("POST", f"/api/v1/resource-units/{uid}/available", {})


class ReadinessAndReplacementTest(HttpIntegrationTest):
    def _plan_with_radios(self, qty=2, section="SECTION_Z1"):
        p = self.create_plan(section=section, start=W_S, end=W_E)
        s, _, body = self.reserve(p["id"], {"items": [
            {"resource_code": "RADIO_SET", "quantity": qty}]})
        self.assertEqual(s, 201, body)
        return p, body["reservation"]["lines"][0]

    def test_prestart_failure_blocks_item_by_item(self):
        p, line = self._plan_with_radios(qty=2)
        held = line["unit_ids"]

        # 一台实例维修失效 + 另一台资源层下线（这里只让单台失效，验证逐项列出）
        s, _, _ = self.request("POST", f"/api/v1/resource-units/{held[0]}/maintenance", {})
        self.assertEqual(s, 200)

        s, _, body = self.request("GET", f"/api/v1/plans/{p['id']}/readiness")
        self.assertEqual(s, 200)
        self.assertFalse(body["ready"])
        blockers = body["blockers"]
        self.assertEqual(len(blockers), 1)
        self.assertEqual(blockers[0]["reason"], "UNIT_MAINTENANCE")
        self.assertEqual(blockers[0]["unit_id"], held[0])
        self.assertEqual(blockers[0]["line_id"], line["line_id"])

        # 开工必须被明确阻断
        s, _, body = self.request("POST", f"/api/v1/plans/{p['id']}/start", {})
        self.assertEqual(s, 409)
        self.assertEqual(body["error"]["code"], "RESOURCE_NOT_READY")
        self.assertEqual(len(body["error"]["details"]), 1)

        # 阻断事件被审计
        denied = self.db_query_all(
            "SELECT COUNT(*) n FROM audit_log WHERE action='START_BLOCKED' AND plan_id=?",
            (p["id"],))[0]["n"]
        self.assertEqual(denied, 1)

    def test_resource_offline_before_start_blocks(self):
        p, _ = self._plan_with_radios(qty=1, section="SECTION_Z2")
        s, _, _ = self.request("POST", "/api/v1/resources/RADIO_SET/offline", {})
        self.assertEqual(s, 200)
        s, _, body = self.request("POST", f"/api/v1/plans/{p['id']}/start", {})
        self.assertEqual(s, 409)
        self.assertEqual(body["error"]["code"], "RESOURCE_NOT_READY")
        self.assertEqual(body["error"]["details"][0]["reason"], "RESOURCE_OFFLINE")
        self.request("POST", "/api/v1/resources/RADIO_SET/online", {})

    def test_same_resource_unit_replacement_recovers_start(self):
        p, line = self._plan_with_radios(qty=2)
        held = line["unit_ids"]
        self.request("POST", f"/api/v1/resource-units/{held[0]}/maintenance", {})

        spare = [u["id"] for u in self.list_units("RADIO_SET") if u["id"] not in held][0]
        s, _, body = self.request("POST", f"/api/v1/plans/{p['id']}/replacements", {
            "line_id": line["line_id"],
            "old_unit_id": held[0],
            "new_unit_id": spare,
        })
        self.assertEqual(s, 200, body)
        self.assertTrue(body["ready"])
        self.assertEqual(body["replacement"]["old_unit_id"], held[0])
        self.assertEqual(body["replacement"]["new_unit_id"], spare)

        s, _, body = self.request("POST", f"/api/v1/plans/{p['id']}/start", {})
        self.assertEqual(s, 200, body)
        self.assertEqual(body["status"], "IN_PROGRESS")

        # 失效实例的旧承诺已解除（不再出现在该行）
        allocs = self.db_query_all(
            "SELECT unit_id FROM allocations WHERE line_id=?", (line["line_id"],))
        ids = {r["unit_id"] for r in allocs}
        self.assertNotIn(held[0], ids)
        self.assertIn(spare, ids)

    def test_alternative_resource_replacement_recovers_start(self):
        p, line = self._plan_with_radios(qty=2, section="SECTION_Z3")
        # 资源整体下线
        self.request("POST", "/api/v1/resources/RADIO_SET/offline", {})
        s, _, ready = self.request("GET", f"/api/v1/plans/{p['id']}/readiness")
        self.assertFalse(ready["ready"])

        s, _, body = self.request("POST", f"/api/v1/plans/{p['id']}/replacements", {
            "line_id": line["line_id"],
            "replacement_resource_code": "MASK_N95",
            "quantity": 2,
        })
        self.assertEqual(s, 200, body)
        self.assertTrue(body["ready"], body)
        self.assertEqual(body["replacement"]["replacement_resource_code"], "MASK_N95")
        self.assertEqual(len(body["replacement"]["unit_ids"]), 2)

        s, _, body = self.request("POST", f"/api/v1/plans/{p['id']}/start", {})
        self.assertEqual(s, 200, body)
        # 旧行 REPLACED、新行 HELD
        rows = {r["id"]: r for r in self.db_query_all(
            "SELECT id,status FROM reservation_lines WHERE request_id="
            "(SELECT id FROM reservation_requests WHERE plan_id=?)", (p["id"],))}
        statuses = sorted(r["status"] for r in rows.values())
        self.assertEqual(statuses, ["HELD", "REPLACED"])

    def test_replace_into_busy_unit_blocked_by_trigger(self):
        p1, l1 = self._plan_with_radios(qty=4, section="SECTION_Z4")  # 占满 4 台
        p2 = self.create_plan(section="SECTION_Z5", start=W_S, end=W_E)
        s, _, b2 = self.reserve(p2["id"], {"items": [
            {"resource_code": "MASK_N95", "quantity": 1}]})
        self.assertEqual(s, 201)
        # 试图把 p2 替换到已占满的 RADIO_SET -> 触发器/库存检查拒绝
        s, _, body = self.request("POST", f"/api/v1/plans/{p2['id']}/replacements", {
            "line_id": b2["reservation"]["lines"][0]["line_id"],
            "replacement_resource_code": "RADIO_SET", "quantity": 1,
        })
        self.assertEqual(s, 409)
        self.assertIn(body["error"]["code"], ("RESOURCE_EXHAUSTED", "PERSISTENCE_ERROR"))


class ReleaseTest(HttpIntegrationTest):
    def test_release_only_once_and_concurrent(self):
        p = self.create_plan(section="SECTION_Z1", start=W_S, end=W_E)
        s, _, body = self.reserve(p["id"], {"items": [
            {"resource_code": "OXYGEN_KIT", "quantity": 3}]})
        self.assertEqual(s, 201)
        units = body["reservation"]["lines"][0]["unit_ids"]

        # 并发双销记：只有一个真正生效
        with ThreadPoolExecutor(max_workers=6) as pool:
            outs = list(pool.map(
                lambda _: self.request("POST", f"/api/v1/plans/{p['id']}/release", {}),
                range(6)))

        true_release = [o for o in outs if o[2].get("released") is True]
        already = [o for o in outs if o[2].get("already_released") is True]
        self.assertEqual(len(true_release), 1, outs)
        self.assertEqual(len(already), 5)

        # 审计中 RELEASED 仅一条
        n = self.db_query_all(
            "SELECT COUNT(*) n FROM audit_log WHERE action='RELEASED' AND plan_id=?",
            (p["id"],))[0]["n"]
        self.assertEqual(n, 1)

        # 释放后实例恢复可订（释放前同窗口不可订）
        p2 = self.create_plan(section="SECTION_Z2", start=W_S, end=W_E)
        s, _, body = self.reserve(p2["id"], {"items": [
            {"resource_code": "OXYGEN_KIT", "quantity": 3}]})
        self.assertEqual(s, 201, body)
        self.assertEqual(sorted(body["reservation"]["lines"][0]["unit_ids"]),
                         sorted(units))

    def test_release_requires_reservation(self):
        p = self.create_plan(section="SECTION_Z1", start=W_S, end=W_E)
        s, _, body = self.request("POST", f"/api/v1/plans/{p['id']}/release", {})
        self.assertEqual(s, 404)
        self.assertEqual(body["error"]["code"], "RESERVATION_NOT_FOUND")
