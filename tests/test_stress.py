"""压力验收：高并发争抢下库存严格不超卖（可重复运行）。"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from tests.base import HttpIntegrationTest

W_S = "2027-02-01T00:00:00Z"
W_E = "2027-02-01T03:00:00Z"


class OversellStressTest(HttpIntegrationTest):
    def test_20_concurrent_requests_never_oversell(self):
        # OXYGEN_KIT 容量 3；20 个互斥矩阵允许并行的计划同时争抢各 1 件
        # 9 个互斥矩阵允许并行的区段（SECTION_D + Z1..Z8）
        sections = ["SECTION_D"] + [f"SECTION_Z{i}" for i in range(1, 9)]
        n_plans = len(sections)
        plans = [self.create_plan(section=sections[i], name=f"stress-{i}",
                                  start=W_S, end=W_E)
                 for i in range(n_plans)]
        barrier = threading.Barrier(n_plans)

        def fire(p):
            barrier.wait()
            return self.request("POST", f"/api/v1/plans/{p['id']}/reservations",
                                body={"items": [
                                    {"resource_code": "OXYGEN_KIT", "quantity": 1}]},
                                timeout=60)

        with ThreadPoolExecutor(max_workers=n_plans) as pool:
            results = list(pool.map(fire, plans))

        winners = [r for r in results if r[0] == 201]
        losers = [r for r in results if r[0] == 409]
        self.assertEqual(len(winners), 3, [r[2] for r in results])
        self.assertEqual(len(losers), n_plans - 3)
        for _, _, body in losers:
            self.assertEqual(body["error"]["code"], "RESOURCE_EXHAUSTED")

        # 数据库里同窗口同资源的有效承诺严格等于容量，实例互不相同
        rows = self.db_query_all(
            """SELECT a.unit_id FROM allocations a
               JOIN reservation_lines l ON l.id = a.line_id
               WHERE a.resource_code='OXYGEN_KIT' AND l.status='HELD'
                 AND a.window_start=? AND a.window_end=?""",
            (W_S, W_E))
        self.assertEqual(len(rows), 3)
        self.assertEqual(len({r["unit_id"] for r in rows}), 3)

        # 库存视图自洽
        r = self.get_resource("OXYGEN_KIT")
        self.assertEqual(r["total_units"], 3)
        self.assertEqual(r["maintenance_units"], 0)

    def test_20_concurrent_requests_then_release_allows_reuse(self):
        sections = ["SECTION_D"] + [f"SECTION_Z{i}" for i in range(1, 9)]
        n_plans = len(sections)
        plans = [self.create_plan(section=sections[i], name=f"reuse-{i}",
                                  start=W_S, end=W_E)
                 for i in range(n_plans)]
        barrier = threading.Barrier(n_plans)

        def fire(p):
            barrier.wait()
            return self.request("POST", f"/api/v1/plans/{p['id']}/reservations",
                                body={"items": [
                                    {"resource_code": "GENSET_5KW", "quantity": 1}]},
                                timeout=60)

        with ThreadPoolExecutor(max_workers=n_plans) as pool:
            results = list(pool.map(fire, plans))
        winners = [p for p, r in zip(plans, results) if r[0] == 201]
        self.assertEqual(len(winners), 2)

        # 赢家（2 个）销记后，窗口容量完整回来
        for p in winners:
            s, _, body = self.request("POST", f"/api/v1/plans/{p['id']}/release", {})
            self.assertEqual(s, 200, body)
            self.assertTrue(body["released"])

        # 赢家销记后计划变为 COMPLETED，其区段重新可用；在赢家区段建迟到计划
        winner_sections = {sections[plans.index(p)] for p in winners}
        p_late = self.create_plan(section=next(iter(winner_sections)), name="late",
                                  start=W_S, end=W_E)
        s, _, body = self.request("POST", f"/api/v1/plans/{p_late['id']}/reservations",
                                  {"items": [
                                      {"resource_code": "GENSET_5KW", "quantity": 2}]})
        self.assertEqual(s, 201, body)
