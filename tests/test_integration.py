import http.client
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.app import serve
from app.service import ReservationService


class ApiClient:
    def __init__(self, server):
        self.server = server

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection(self.server.server_address[0], self.server.server_address[1], timeout=30)
        try:
            data = None
            req_headers = headers.copy() if headers else {}
            if body is not None:
                data = json.dumps(body).encode("utf-8")
                req_headers["Content-Type"] = "application/json"
            conn.request(method, path, data, req_headers)
            response = conn.getresponse()
            raw = response.read().decode("utf-8")
            payload = json.loads(raw) if raw else None
            return response.status, payload
        finally:
            conn.close()


class ReservationIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "reservation.db"
        self.server, self.service = serve(str(self.db_path), port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.api = ApiClient(self.server)
        status, body = self.api.request("POST", "/resources", {
            "sku": "MASK",
            "name": "Protective mask",
            "quantity": 1,
        })
        self.assertEqual(status, 201, body)
        status, body = self.api.request("POST", "/resources", {
            "sku": "RADIO",
            "name": "Drill radio",
            "quantity": 2,
        })
        self.assertEqual(status, 201, body)

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.service.close()
        self.tempdir.cleanup()

    def reserve(self, plan_id, key, start, end, sku="MASK", quantity=1, segment="SECTION_A"):
        return self.api.request("POST", "/reservations", {
            "plan_id": plan_id,
            "idempotency_key": key,
            "segment": segment,
            "start_at": start,
            "end_at": end,
            "requirements": [{"sku": sku, "quantity": quantity}],
        })

    def test_openapi_health_and_existing_segment_rules_are_preserved(self):
        status, body = self.api.request("GET", "/healthz")
        self.assertEqual((status, body["status"]), (200, "ok"))

        status, spec = self.api.request("GET", "/openapi.json")
        self.assertEqual(status, 200)
        self.assertEqual(spec["openapi"], "3.0.3")
        self.assertIn("/reservations", spec["paths"])
        self.assertIn("ReservationRequest", spec["components"]["schemas"])

        status, body = self.api.request("GET", "/segment-rules")
        self.assertEqual(status, 200)
        rules = {(r["segment_a"], r["segment_b"]): r["can_parallel"] for r in body["rules"]}
        self.assertTrue(rules[("SECTION_A", "SECTION_B")])
        self.assertTrue(rules[("SECTION_A", "SECTION_C")])
        self.assertFalse(rules[("SECTION_A", "SECTION_A")])
        self.assertNotIn(("SECTION_A", "CUSTOM"), rules)

    def test_overlapping_window_contention_only_one_plan_succeeds(self):
        start = "2030-01-01T10:00:00Z"
        end = "2030-01-01T11:00:00Z"
        results = []
        barrier = threading.Barrier(8)

        def worker(index):
            barrier.wait()
            results.append(self.reserve(
                f"contend-{index}", f"contend-key-{index}", start, end,
                segment=f"CONTEND-{index}",
            ))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        statuses = [status for status, _ in results]
        self.assertEqual(statuses.count(201), 1, results)
        self.assertEqual(statuses.count(409), 7, results)
        failures = [body for status, body in results if status == 409]
        self.assertTrue(all(body["error"]["code"] == "resource_unavailable" for body in failures))
        self.assertEqual(failures[0]["error"]["details"]["shortages"][0]["available"], 0)

        _, plans = self.api.request("GET", "/reservations")
        self.assertEqual(len(plans["plans"]), 1)

    def test_adjacent_windows_reuse_same_resource_and_compete_correctly(self):
        first = ("2030-02-01T10:00:00Z", "2030-02-01T11:00:00Z")
        second = ("2030-02-01T11:00:00Z", "2030-02-01T12:00:00Z")
        overlapping = ("2030-02-01T10:30:00Z", "2030-02-01T11:30:00Z")

        status, body1 = self.reserve("adjacent-1", "adj-1", *first, segment="SECTION_A")
        self.assertEqual(status, 201, body1)
        status, body2 = self.reserve("adjacent-2", "adj-2", *second, segment="SECTION_B")
        self.assertEqual(status, 201, body2)
        self.assertEqual(body1["allocations"][0]["unit_id"], body2["allocations"][0]["unit_id"])

        status, body3 = self.reserve("overlap", "overlap-key", *overlapping, segment="SECTION_C")
        self.assertEqual(status, 409, body3)
        self.assertEqual(body3["error"]["code"], "resource_unavailable")

        # Both adjacent reservations are also accepted under real concurrency.
        results = []
        barrier = threading.Barrier(2)

        def adjacent_worker(plan_id, key, window):
            barrier.wait()
            results.append(self.reserve(plan_id, key, *window, sku="RADIO"))

        t1 = threading.Thread(target=adjacent_worker, args=(
            "radio-a", "radio-a-key", ("2031-03-01T08:00:00Z", "2031-03-01T09:00:00Z")))
        t2 = threading.Thread(target=adjacent_worker, args=(
            "radio-b", "radio-b-key", ("2031-03-01T09:00:00Z", "2031-03-01T10:00:00Z")))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(sorted(status for status, _ in results), [201, 201], results)

    def test_idempotent_retry_does_not_consume_capacity_twice(self):
        window = ("2030-04-01T09:00:00Z", "2030-04-01T10:00:00Z")
        payload = {
            "plan_id": "idem-plan",
            "idempotency_key": "same-key",
            "segment": "SECTION_A",
            "start_at": window[0],
            "end_at": window[1],
            "requirements": [{"sku": "RADIO", "quantity": 2}],
        }
        first = self.api.request("POST", "/reservations", payload)
        second = self.api.request("POST", "/reservations", payload)
        third = self.api.request("POST", "/reservations", payload)
        self.assertEqual(first[0], 201, first[1])
        self.assertEqual((second[0], second[1]["replayed"]), (200, True))
        self.assertEqual((third[0], third[1]["replayed"]), (200, True))
        self.assertEqual(first[1]["plan_id"], second[1]["plan_id"])
        self.assertEqual(len(second[1]["allocations"]), 2)

        _, plans = self.api.request("GET", "/reservations")
        self.assertEqual(len(plans["plans"]), 1)
        with self.service.conn:
            count = self.service.conn.execute("SELECT COUNT(*) AS c FROM reservation_items").fetchone()["c"]
        self.assertEqual(count, 2)

        changed_payload = dict(payload, end_at="2030-04-01T10:30:00Z")
        status, body = self.api.request("POST", "/reservations", changed_payload)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "idempotency_conflict")

    def test_concurrent_requests_never_oversell(self):
        window = ("2032-05-01T09:00:00Z", "2032-05-01T10:00:00Z")
        results = []
        barrier = threading.Barrier(12)

        def worker(index):
            barrier.wait()
            results.append(self.reserve(
                f"radio-{index}", f"radio-key-{index}", *window,
                sku="RADIO", quantity=1, segment=f"RADIO-{index}",
            ))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        statuses = [status for status, _ in results]
        self.assertEqual(statuses.count(201), 2, results)
        self.assertEqual(statuses.count(409), 10, results)
        for _, body in results:
            if "error" in body:
                shortage = body["error"]["details"]["shortages"][0]
                self.assertEqual(shortage["requested"], 1)
                self.assertLess(shortage["available"], 2)

        rows = self.service.conn.execute(
            """SELECT unit_id, COUNT(*) AS c
                 FROM reservation_items WHERE active = 1
                GROUP BY unit_id"""
        ).fetchall()
        self.assertEqual({row["unit_id"]: row["c"] for row in rows}, {"RADIO-001": 1, "RADIO-002": 1})

    def test_segment_matrix_allows_parallel_but_resource_scarcity_still_blocks(self):
        window = ("2033-06-01T09:00:00Z", "2033-06-01T10:00:00Z")
        status, first = self.reserve("seg-a", "seg-a-key", *window, segment="SECTION_A")
        self.assertEqual(status, 201, first)

        # A and B may run in parallel, but they cannot both hold the one mask.
        status, second = self.reserve("seg-b", "seg-b-key", *window, segment="SECTION_B")
        self.assertEqual(status, 409, second)
        self.assertEqual(second["error"]["code"], "resource_unavailable")

        # A and C also use different resources but same segment? Use same segment
        # to verify existing same-segment mutual exclusion remains enforced.
        status, third = self.reserve("seg-c", "seg-c-key", *window, sku="RADIO", segment="SECTION_A")
        self.assertEqual(status, 409, third)
        self.assertEqual(third["error"]["code"], "segment_conflict")

    def test_prestart_failure_blocks_each_item_and_replacement_resumes_start(self):
        now = datetime.now(timezone.utc)
        start = (now - timedelta(minutes=1)).isoformat()
        end = (now + timedelta(minutes=10)).isoformat()
        status, plan = self.reserve("prestart-plan", "prestart-key", start, end, sku="RADIO")
        self.assertEqual(status, 201, plan)
        units = [item["unit_id"] for item in plan["allocations"]]
        old_unit = units[0]

        status, changed = self.api.request("POST", f"/resources/units/{old_unit}/status", {
            "status": "maintenance",
            "reason": "pre-use check failed",
        })
        self.assertEqual(status, 200, changed)
        self.assertEqual(changed["effective_status"], "maintenance")

        status, ready = self.api.request("GET", "/reservations/prestart-plan/readiness")
        self.assertEqual(status, 200)
        self.assertFalse(ready["ready"])
        self.assertEqual(ready["blocked_items"], [{
            "sku": "RADIO",
            "unit_id": old_unit,
            "base_status": "maintenance",
            "reason": "resource_unavailable_before_start",
        }])

        status, blocked = self.api.request("POST", "/reservations/prestart-plan/start", {})
        self.assertEqual(status, 409, blocked)
        self.assertEqual(blocked["error"]["code"], "prestart_blocked")
        self.assertEqual(len(blocked["error"]["details"]["blocked_items"]), 1)

        # Expand inventory, replace the failed item explicitly, and start.
        status, expanded = self.api.request("POST", "/resources/RADIO/expand", {"quantity": 1})
        self.assertEqual(status, 200, expanded)
        spare = expanded["unit_ids"][0]
        status, replacement = self.api.request("POST", "/reservations/prestart-plan/replacements", {
            "old_unit_id": old_unit,
            "replacement_unit_id": spare,
            "reason": "battery damaged",
            "idempotency_key": "replace-1",
        })
        self.assertEqual(status, 200, replacement)
        self.assertFalse(replacement["replayed"])

        status, replay = self.api.request("POST", "/reservations/prestart-plan/replacements", {
            "old_unit_id": old_unit,
            "replacement_unit_id": spare,
            "reason": "battery damaged",
            "idempotency_key": "replace-1",
        })
        self.assertEqual((status, replay["replayed"]), (200, True))

        status, ready = self.api.request("GET", "/reservations/prestart-plan/readiness")
        self.assertEqual(status, 200)
        self.assertTrue(ready["ready"], ready)
        status, started = self.api.request("POST", "/reservations/prestart-plan/start", {})
        self.assertEqual(status, 200, started)
        self.assertFalse(started["already_started"])
        self.assertEqual(started["status"], "in_progress")

        # An occupied in-progress unit cannot be pushed into maintenance.
        active_unit = [i["unit_id"] for i in started["allocations"] if i["unit_id"] != old_unit][0]
        status, denied = self.api.request("POST", f"/resources/units/{active_unit}/status", {"status": "maintenance"})
        self.assertEqual(status, 409)
        self.assertEqual(denied["error"]["code"], "resource_in_use")

        # Repair of the detached failed unit returns it to inventory.
        status, repaired = self.api.request("POST", f"/resources/units/{old_unit}/status", {"status": "available"})
        self.assertEqual(status, 200)
        self.assertEqual(repaired["effective_status"], "available")

    def test_release_happens_exactly_once_and_audits_once(self):
        window = ("2034-07-01T09:00:00Z", "2034-07-01T10:00:00Z")
        status, plan = self.reserve("release-plan", "release-key", *window, sku="RADIO")
        self.assertEqual(status, 201, plan)
        units = sorted(item["unit_id"] for item in plan["allocations"])

        status, release1 = self.api.request("POST", "/reservations/release-plan/release", {})
        self.assertEqual(status, 200, release1)
        self.assertFalse(release1["already_released"])
        self.assertEqual(sorted(release1["released_units"]), units)

        status, release2 = self.api.request("POST", "/reservations/release-plan/release", {})
        self.assertEqual(status, 200, release2)
        self.assertTrue(release2["already_released"])
        self.assertEqual(release2["released_units"], [])

        status, audit = self.api.request("GET", "/audit-logs?plan_id=release-plan")
        self.assertEqual(status, 200)
        release_events = [entry for entry in audit["audit"] if entry["event_type"] == "reservation_released"]
        self.assertEqual(len(release_events), 1)
        self.assertEqual(sorted(release_events[0]["detail"]["released_units"]), units)

        # Released inventory is reusable even at the same historical time window.
        status, again = self.reserve("reuse-plan", "reuse-key", *window, sku="RADIO", quantity=2)
        self.assertEqual(status, 201, again)

    def test_database_trigger_blocks_overlapping_inserts_directly(self):
        w1 = ("2036-09-01T08:00:00Z", "2036-09-01T09:00:00Z")
        status, p1 = self.reserve("trig-1", "trig-1-key", *w1, sku="RADIO", segment="TRIG-1")
        self.assertEqual(status, 201, p1)
        # A second plan exists (holding a different SKU) so the foreign key is
        # satisfied, but it has no RADIO-001 allocation of its own.
        status, p2 = self.reserve("trig-2", "trig-2-key", *w1, sku="MASK", segment="TRIG-2")
        self.assertEqual(status, 201, p2)

        # Bypass the service: an active allocation of RADIO-001 for plan
        # trig-2 that overlaps trig-1 must be rejected by the SQLite trigger.
        conn = sqlite3.connect(str(self.db_path), timeout=5)
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA foreign_keys = ON")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                """INSERT INTO reservation_items(plan_id, sku, unit_id, start_at, end_at, active)
                   VALUES ('trig-2', 'RADIO', 'RADIO-001',
                           '2036-09-01T08:30:00Z', '2036-09-01T09:30:00Z', 1)"""
            )
        conn.close()

    def test_restart_restores_inventory_reservations_and_history(self):
        a_window = ("2035-08-01T08:00:00Z", "2035-08-01T09:00:00Z")
        b_window = ("2035-08-01T09:00:00Z", "2035-08-01T10:00:00Z")
        status, a = self.reserve("persist-a", "persist-a-key", *a_window)
        self.assertEqual(status, 201, a)
        status, b = self.reserve("persist-b", "persist-b-key", *b_window)
        self.assertEqual(status, 201, b)
        status, released = self.api.request("POST", "/reservations/persist-a/release", {})
        self.assertEqual(status, 200, released)
        old_inventory = self.api.request("GET", "/resources")[1]
        old_plans = self.api.request("GET", "/reservations")[1]
        old_audit = self.api.request("GET", "/audit-logs")[1]

        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.service.close()

        self.server, self.service = serve(str(self.db_path), port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.api = ApiClient(self.server)

        new_inventory = self.api.request("GET", "/resources")[1]
        new_plans = self.api.request("GET", "/reservations")[1]
        new_audit = self.api.request("GET", "/audit-logs")[1]
        self.assertEqual(new_inventory, old_inventory)
        self.assertEqual(new_plans, old_plans)
        self.assertEqual(new_audit, old_audit)

        plan_a = next(p for p in new_plans["plans"] if p["plan_id"] == "persist-a")
        plan_b = next(p for p in new_plans["plans"] if p["plan_id"] == "persist-b")
        self.assertEqual(plan_a["status"], "released")
        self.assertEqual(plan_b["status"], "reserved")
        self.assertEqual(len(plan_a["allocations"]), 0)
        self.assertEqual(len(plan_b["allocations"]), 1)

        # A released unit is reusable after restart; B's future adjacent unit remains held.
        status, reused = self.reserve("persist-reuse", "persist-reuse-key", *a_window)
        self.assertEqual(status, 201, reused)


if __name__ == "__main__":
    unittest.main()
