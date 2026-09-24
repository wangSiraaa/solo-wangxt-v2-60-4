"""验收：既有区段互斥规则不被资源预约能力改变。"""
from tests.base import HttpIntegrationTest


class SectionMutexTest(HttpIntegrationTest):
    def test_same_section_overlap_rejected(self):
        # 同一区段重叠窗口 -> 互斥
        self.create_plan(section="SECTION_A",
                         start="2026-10-01T00:00:00Z", end="2026-10-01T02:00:00Z")
        status, _, body = self.request("POST", "/api/v1/plans", {
            "name": "p2", "section": "SECTION_A",
            "window_start": "2026-10-01T01:00:00Z",
            "window_end": "2026-10-01T03:00:00Z",
        })
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "SECTION_MUTEX_CONFLICT")

    def test_indoor_sections_pairwise_mutex(self):
        # A/B/C 两两互斥
        self.create_plan(section="SECTION_B",
                         start="2026-11-01T00:00:00Z", end="2026-11-01T02:00:00Z")
        status, _, body = self.request("POST", "/api/v1/plans", {
            "name": "p", "section": "SECTION_C",
            "window_start": "2026-11-01T01:00:00Z",
            "window_end": "2026-11-01T03:00:00Z",
        })
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "SECTION_MUTEX_CONFLICT")

    def test_open_zones_allowed_in_parallel_but_resources_still_guarded(self):
        # 互斥矩阵允许并行的区段（两个露天集结区）
        p1 = self.create_plan(section="SECTION_Z1",
                              start="2026-12-01T00:00:00Z", end="2026-12-01T02:00:00Z")
        p2 = self.create_plan(section="SECTION_Z2",
                              start="2026-12-01T00:00:00Z", end="2026-12-01T02:00:00Z")
        self.assertNotEqual(p1["id"], p2["id"])

        # 但同一稀缺资源不超卖（GENSET_5KW 仅 2 台）
        s1, _, b1 = self.reserve(p1["id"], {"items": [
            {"resource_code": "GENSET_5KW", "quantity": 2}]})
        self.assertEqual(s1, 201, b1)
        s2, _, b2 = self.reserve(p2["id"], {"items": [
            {"resource_code": "GENSET_5KW", "quantity": 1}]})
        self.assertEqual(s2, 409)
        self.assertEqual(b2["error"]["code"], "RESOURCE_EXHAUSTED")
        self.assertEqual(b2["error"]["details"][0]["available"], 0)

    def test_touching_windows_not_mutex(self):
        # 首尾相接的窗口不构成区段冲突
        self.create_plan(section="SECTION_A",
                         start="2027-01-01T00:00:00Z", end="2027-01-01T02:00:00Z")
        status, _, body = self.request("POST", "/api/v1/plans", {
            "name": "p2", "section": "SECTION_A",
            "window_start": "2027-01-01T02:00:00Z",
            "window_end": "2027-01-01T04:00:00Z",
        })
        self.assertEqual(status, 201, body)

    def test_mutex_matrix_endpoint_unchanged(self):
        status, _, body = self.request("GET", "/api/v1/sections")
        self.assertEqual(status, 200)
        m = body["mutex_matrix"]
        self.assertTrue(m["SECTION_A"]["SECTION_A"])
        self.assertTrue(m["SECTION_A"]["SECTION_B"])
        self.assertFalse(m["SECTION_Z1"]["SECTION_Z2"])
        self.assertFalse(m["SECTION_D"]["SECTION_A"])
