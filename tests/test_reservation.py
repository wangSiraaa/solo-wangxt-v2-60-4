"""验收：重叠窗口竞争、首尾相接复用、幂等、并发不超卖、库存一致。"""
from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor

from tests.base import HttpIntegrationTest

W_S = "2026-10-10T00:00:00Z"
W_E = "2026-10-10T02:00:00Z"
ITEMS = lambda code, qty=1: {"items": [{"resource_code": code, "quantity": qty}]}


class OverlapRaceTest(HttpIntegrationTest):
    def test_overlapping_windows_only_one_plan_wins(self):
        # 两台发电机，三个允许并行的计划各要 1 台，完全重叠窗口
        plans = [self.create_plan(section=f"SECTION_Z{i+1}", start=W_S, end=W_E)
                 for i in range(3)]

        results = []

        def reserve(p):
            return self.reserve(p["id"], ITEMS("GENSET_5KW", 1))

        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(reserve, plans))

        winners = [r for r in results if r[0] == 201]
        losers = [r for r in results if r[0] == 409]
        self.assertEqual(len(winners), 2, [r[2] for r in results])
        self.assertEqual(len(losers), 1)
        self.assertEqual(losers[0][2]["error"]["code"], "RESOURCE_EXHAUSTED")

        # 持久化核验：有效承诺恰好 2 条，实例互不重复
        won_lines = [w[2]["reservation"]["lines"][0] for w in winners]
        allocs = self.db_query_all(
            """SELECT a.unit_id FROM allocations a
               JOIN reservation_lines l ON l.id=a.line_id
               WHERE l.status='HELD' AND a.resource_code='GENSET_5KW'
                 AND a.window_start=? AND a.window_end=?""",
            (W_S, W_E),
        )
        self.assertEqual(sorted(r["unit_id"] for r in allocs),
                         sorted(uid for line in won_lines for uid in line["unit_ids"]))
        self.assertEqual(len({r["unit_id"] for r in allocs}), 2)


class BackToBackTest(HttpIntegrationTest):
    def test_back_to_back_window_reuses_units(self):
        # [00:00,02:00) 用完 2 台；[02:00,04:00) 必须能再订同样 2 台（半开区间）
        p1 = self.create_plan(section="SECTION_Z1",
                              start="2026-10-11T00:00:00Z", end="2026-10-11T02:00:00Z")
        p2 = self.create_plan(section="SECTION_Z2",
                              start="2026-10-11T02:00:00Z", end="2026-10-11T04:00:00Z")
        s1, _, b1 = self.reserve(p1["id"], ITEMS("GENSET_5KW", 2))
        s2, _, b2 = self.reserve(p2["id"], ITEMS("GENSET_5KW", 2))
        self.assertEqual((s1, s2), (201, 201), (b1, b2))
        self.assertEqual(
            sorted(b1["reservation"]["lines"][0]["unit_ids"]),
            sorted(b2["reservation"]["lines"][0]["unit_ids"]),
        )

        # 真正重叠（早一分钟）则必须失败
        p3 = self.create_plan(section="SECTION_Z3",
                              start="2026-10-11T01:59:00Z", end="2026-10-11T03:00:00Z")
        s3, _, b3 = self.reserve(p3["id"], ITEMS("GENSET_5KW", 1))
        self.assertEqual(s3, 409)
        self.assertEqual(b3["error"]["code"], "RESOURCE_EXHAUSTED")


class IdempotencyTest(HttpIntegrationTest):
    def test_repeated_reserve_same_key_does_not_double_allocate(self):
        p = self.create_plan(section="SECTION_Z1", start=W_S, end=W_E)
        body = ITEMS("MASK_N95", 3)
        raw = json.dumps(body)

        s1, h1, b1 = self.reserve(p["id"], None, key="idem-001", raw_body=raw)
        s2, h2, b2 = self.reserve(p["id"], None, key="idem-001", raw_body=raw)
        s3, _, b3 = self.reserve(p["id"], None, key="idem-001", raw_body=raw)

        self.assertEqual((s1, s2, s3), (201, 201, 201))
        self.assertIsNone(h1.get("Idempotency-Replayed"))
        self.assertEqual(h2.get("Idempotency-Replayed"), "true")
        self.assertTrue(b2["replayed"] and b3["replayed"])
        units_first = b1["reservation"]["lines"][0]["unit_ids"]
        for b in (b2, b3):
            self.assertEqual(b["reservation"]["lines"][0]["unit_ids"], units_first)

        # 只有一条有效预约单、三条承诺
        lines = self.db_query_all(
            "SELECT id FROM reservation_lines WHERE request_id=(SELECT id FROM reservation_requests WHERE plan_id=?)",
            (p["id"],))
        self.assertEqual(len(lines), 1)
        allocs = self.db_query_all(
            "SELECT COUNT(*) n FROM allocations WHERE line_id=?", (lines[0]["id"],))
        self.assertEqual(allocs[0]["n"], 3)

    def test_duplicate_reserve_without_key_rejected(self):
        p = self.create_plan(section="SECTION_Z1", start=W_S, end=W_E)
        s1, _, _ = self.reserve(p["id"], ITEMS("MASK_N95", 1))
        self.assertEqual(s1, 201)
        s2, _, b2 = self.reserve(p["id"], ITEMS("MASK_N95", 1))
        self.assertEqual(s2, 409)
        self.assertEqual(b2["error"]["code"], "PLAN_ALREADY_RESERVED")

    def test_idempotency_key_reuse_with_different_body_conflicts(self):
        p = self.create_plan(section="SECTION_Z1", start=W_S, end=W_E)
        s1, _, _ = self.reserve(p["id"], None, key="idem-x",
                                raw_body=json.dumps(ITEMS("MASK_N95", 1)))
        self.assertEqual(s1, 201)
        s2, _, b2 = self.reserve(p["id"], None, key="idem-x",
                                 raw_body=json.dumps(ITEMS("RADIO_SET", 1)))
        self.assertEqual(s2, 409)
        self.assertEqual(b2["error"]["code"], "IDEMPOTENCY_CONFLICT")

    def test_concurrent_same_key_single_winner(self):
        # 同一幂等键并发：资源容量 3，但 6 个同键并发请求只能产生一次占用
        plans = [self.create_plan(section=f"SECTION_Z{i+1}", start=W_S, end=W_E)
                 for i in range(3)]
        target = plans[0]["id"]
        raw = json.dumps(ITEMS("OXYGEN_KIT", 3))

        outcomes = []
        lock = threading.Lock()

        def fire(i):
            # 全部打到同一个计划/同一个幂等键
            s, _, b = self.reserve(target, None, key="race-key", raw_body=raw)
            with lock:
                outcomes.append((s, b))

        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(fire, range(6)))

        self.assertTrue(all(s == 201 for s, _ in outcomes), outcomes)
        self.assertTrue(all(b["replayed"] in (True, False) for _, b in outcomes))
        lines = self.db_query_all(
            "SELECT l.id FROM reservation_lines l "
            "JOIN reservation_requests q ON q.id=l.request_id WHERE q.plan_id=?",
            (target,))
        self.assertEqual(len(lines), 1)
        n = self.db_query_all("SELECT COUNT(*) n FROM allocations WHERE line_id=?",
                              (lines[0]["id"],))[0]["n"]
        self.assertEqual(n, 3)


class NoOversellUnderHighConcurrencyTest(HttpIntegrationTest):
    def test_eight_parallel_plans_three_units_exactly_three_win(self):
        # 容量=3，8 个允许并行计划并发争抢，每个要 1 件
        plans = [self.create_plan(section=f"SECTION_Z{i+1}", start=W_S, end=W_E)
                 for i in range(8)]
        barrier = threading.Barrier(8)

        def fire(p):
            barrier.wait()  # 尽量同时发起，制造真正竞争
            return self.reserve(p["id"], ITEMS("OXYGEN_KIT", 1), timeout=60)

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(fire, plans))

        winners = [r for r in results if r[0] == 201]
        losers = [r for r in results if r[0] == 409]
        self.assertEqual(len(winners), 3, [r[2] for r in results])
        self.assertEqual(len(losers), 5)
        won_units = [w[2]["reservation"]["lines"][0]["unit_ids"][0] for w in winners]
        self.assertEqual(len(set(won_units)), 3, "占用实例不得重复")
        # 每个失败方都有 DENIED 审计
        denied = self.db_query_all(
            "SELECT COUNT(*) n FROM audit_log WHERE action='RESERVE_DENIED'")[0]["n"]
        self.assertGreaterEqual(denied, 5)

    def test_partial_quantity_all_or_nothing(self):
        # 要 2 件但窗口只剩 1 件 -> 整单失败，不得只占 1 件
        p1 = self.create_plan(section="SECTION_Z1", start=W_S, end=W_E)
        s1, _, _ = self.reserve(p1["id"], ITEMS("OXYGEN_KIT", 2))
        self.assertEqual(s1, 201)
        p2 = self.create_plan(section="SECTION_Z2", start=W_S, end=W_E)
        s2, _, b2 = self.reserve(p2["id"], ITEMS("OXYGEN_KIT", 2))
        self.assertEqual(s2, 409)
        self.assertEqual(b2["error"]["details"][0]["available"], 1)
        # p2 没有留下任何预约/占用
        self.assertEqual(self.db_query_all(
            "SELECT COUNT(*) n FROM reservation_requests WHERE plan_id=?", (p2["id"],))[0]["n"], 0)
