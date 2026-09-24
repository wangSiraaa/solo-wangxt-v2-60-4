"""数据访问层：所有 SQL 与库存推导集中于此。"""
from __future__ import annotations

from .errors import ApiError


# 明细行处于“有效占用”的状态（触发器与此集合保持一致）
ACTIVE_LINE_STATUSES = ("HELD",)


class Store:
    def __init__(self, conn):
        self.conn = conn

    # ---------------- 资源 / 实例 ----------------
    def list_resources(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM resources ORDER BY code"
        ).fetchall()
        return [self.get_resource(r["code"], base=r) for r in rows]

    def get_resource_row(self, code: str):
        return self.conn.execute("SELECT * FROM resources WHERE code=?", (code,)).fetchone()

    def get_resource(self, code: str, base=None) -> dict:
        row = base or self.get_resource_row(code)
        if row is None:
            raise ApiError("RESOURCE_NOT_FOUND", f"资源不存在: {code}", 404)
        total = self.conn.execute(
            "SELECT COUNT(*) n FROM resource_units WHERE resource_code=?", (code,)
        ).fetchone()["n"]
        maintenance = self.conn.execute(
            "SELECT COUNT(*) n FROM resource_units WHERE resource_code=? AND status='MAINTENANCE'",
            (code,),
        ).fetchone()["n"]
        occupied = self.count_occupied_units(code)
        online = bool(row["online"])
        if not online:
            effective = "MAINTENANCE"
        elif occupied > 0:
            effective = "OCCUPIED"
        else:
            effective = "AVAILABLE"
        return {
            "code": row["code"],
            "name": row["name"],
            "kind": row["kind"],
            "online": online,
            "version": row["version"],
            "status": effective,
            "total_units": total,
            "maintenance_units": maintenance,
            "occupied_units": occupied,
            # 任意未来/当前窗口可被预订的最大数量（维修与占用均会减少它）
            "available_now": total - maintenance - occupied if online else 0,
            "updated_at": row["updated_at"],
        }

    def count_occupied_units(self, resource_code: str) -> int:
        """当前时刻被有效承诺占用的实例数（OCCUPIED 实时推导，不做物理翻转）。"""
        row = self.conn.execute(
            """
            SELECT COUNT(DISTINCT a.unit_id) n
            FROM allocations a
            JOIN reservation_lines l ON l.id = a.line_id
            JOIN reservation_requests q ON q.id = l.request_id
            WHERE a.resource_code = ? AND l.status = 'HELD'
              AND a.window_start <= ? AND ? < a.window_end
            """,
            (resource_code, self._clock(), self._clock()),
        ).fetchone()
        return row["n"]

    def _clock(self) -> str:
        from .timeutil import now_iso
        return now_iso()

    def create_resource(self, code: str, name: str, kind: str, quantity: int, ts: str):
        try:
            self.conn.execute(
                "INSERT INTO resources(code,name,kind,online,created_at,updated_at) "
                "VALUES (?,?,?,1,?,?)",
                (code, name, kind, ts, ts),
            )
        except Exception as exc:  # 唯一编码冲突
            raise ApiError("RESOURCE_EXISTS", f"资源编码已存在: {code}", 409) from exc
        for seq in range(1, quantity + 1):
            self.conn.execute(
                "INSERT INTO resource_units(resource_code,seq,status,created_at) VALUES(?,?,'AVAILABLE',?)",
                (code, seq, ts),
            )

    def set_resource_online(self, code: str, online: bool, ts: str) -> None:
        row = self.get_resource_row(code)
        if row is None:
            raise ApiError("RESOURCE_NOT_FOUND", f"资源不存在: {code}", 404)
        self.conn.execute(
            "UPDATE resources SET online=?, updated_at=?, version=version+1 WHERE code=?",
            (1 if online else 0, ts, code),
        )

    def get_unit(self, unit_id: int):
        return self.conn.execute(
            """SELECT u.*, r.name resource_name, r.online resource_online
               FROM resource_units u JOIN resources r ON r.code=u.resource_code
               WHERE u.id=?""",
            (unit_id,),
        ).fetchone()

    def list_units(self, resource_code: str | None = None) -> list[dict]:
        sql = (
            "SELECT u.* FROM resource_units u "
            "JOIN resources r ON r.code=u.resource_code "
            "WHERE 1=1"
        )
        args: list = []
        if resource_code:
            sql += " AND u.resource_code=?"
            args.append(resource_code)
        sql += " ORDER BY u.resource_code, u.seq"
        result = []
        from .timeutil import now_iso
        clock = now_iso()
        for u in self.conn.execute(sql, args).fetchall():
            result.append(self._unit_to_dict(u, clock))
        return result

    def _unit_to_dict(self, u, clock: str | None = None) -> dict:
        clock = clock or self._clock()
        stored = u["status"]
        live = self.conn.execute(
            """SELECT 1 FROM allocations a JOIN reservation_lines l ON l.id=a.line_id
               WHERE a.unit_id=? AND l.status='HELD'
                 AND a.window_start<=? AND ?<a.window_end LIMIT 1""",
            (u["id"], clock, clock),
        ).fetchone()
        if stored == "MAINTENANCE":
            effective = "MAINTENANCE"
        elif live:
            effective = "OCCUPIED"
        else:
            effective = "AVAILABLE"
        return {
            "id": u["id"],
            "resource_code": u["resource_code"],
            "seq": u["seq"],
            "status": effective,
        }

    def set_unit_status(self, unit_id: int, status: str, ts: str) -> dict:
        row = self.get_unit(unit_id)
        if row is None:
            raise ApiError("UNIT_NOT_FOUND", f"资源实例不存在: {unit_id}", 404)
        self.conn.execute(
            "UPDATE resource_units SET status=? WHERE id=?", (status, unit_id)
        )
        return self._unit_to_dict(self.get_unit(unit_id))

    def find_bookable_units(
        self, resource_code: str, start: str, end: str, exclude_unit_ids=()
    ) -> list[int]:
        """返回窗口内可预订的实例 id（按序号稳定排序）。"""
        exclude = set(exclude_unit_ids or ())
        rows = self.conn.execute(
            """
            SELECT u.id FROM resource_units u
            JOIN resources r ON r.code = u.resource_code
            WHERE u.resource_code = ?
              AND u.status = 'AVAILABLE'
              AND r.online = 1
              AND NOT EXISTS (
                  SELECT 1 FROM allocations a
                  JOIN reservation_lines l ON l.id = a.line_id
                  WHERE a.unit_id = u.id AND l.status = 'HELD'
                    AND ? < a.window_end AND a.window_start < ?
              )
            ORDER BY u.seq
            """,
            (resource_code, start, end),
        ).fetchall()
        return [r["id"] for r in rows if r["id"] not in exclude]

    # ---------------- 计划 ----------------
    def create_plan(self, name, section, start, end, ts) -> int:
        cur = self.conn.execute(
            "INSERT INTO plans(name,section,window_start,window_end,status,created_at) "
            "VALUES (?,?,?,?,'SCHEDULED',?)",
            (name, section, start, end, ts),
        )
        return cur.lastrowid

    def get_plan(self, plan_id: int):
        row = self.conn.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
        if row is None:
            raise ApiError("PLAN_NOT_FOUND", f"演练计划不存在: {plan_id}", 404)
        return dict(row)

    def list_plans(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM plans ORDER BY id").fetchall()]

    def set_plan_status(self, plan_id: int, status: str) -> None:
        self.conn.execute("UPDATE plans SET status=? WHERE id=?", (status, plan_id))

    def active_plans_except(self, plan_id: int | None) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM plans WHERE status IN ('SCHEDULED','IN_PROGRESS')",
        ).fetchall()
        return [dict(r) for r in rows if r["id"] != plan_id]

    # ---------------- 预约 ----------------
    def get_request_by_plan(self, plan_id: int):
        return self.conn.execute(
            "SELECT * FROM reservation_requests WHERE plan_id=?", (plan_id,)
        ).fetchone()

    def get_request(self, request_id: int):
        return self.conn.execute(
            "SELECT * FROM reservation_requests WHERE id=?", (request_id,)
        ).fetchone()

    def create_request(self, plan_id: int, ts: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO reservation_requests(plan_id,status,created_at) VALUES(?,'CONFIRMED',?)",
            (plan_id, ts),
        )
        return cur.lastrowid

    def create_line(self, request_id, resource_code, quantity, start, end, ts,
                    replaces_line_id=None) -> int:
        cur = self.conn.execute(
            """INSERT INTO reservation_lines
               (request_id,resource_code,quantity,window_start,window_end,status,
                replaces_line_id,created_at)
               VALUES(?,?,?,?,?,'HELD',?,?)""",
            (request_id, resource_code, quantity, start, end, replaces_line_id, ts),
        )
        return cur.lastrowid

    def mark_line_replaced(self, line_id: int) -> None:
        self.conn.execute("UPDATE reservation_lines SET status='REPLACED' WHERE id=?", (line_id,))

    def insert_allocation(self, line_id, unit_id, resource_code, start, end) -> None:
        self.conn.execute(
            "INSERT INTO allocations(line_id,unit_id,resource_code,window_start,window_end) "
            "VALUES (?,?,?,?,?)",
            (line_id, unit_id, resource_code, start, end),
        )

    def get_line(self, line_id: int):
        return self.conn.execute(
            "SELECT * FROM reservation_lines WHERE id=?", (line_id,)
        ).fetchone()

    def list_lines(self, request_id: int) -> list[dict]:
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT * FROM reservation_lines WHERE request_id=? ORDER BY id", (request_id,)
            ).fetchall()
        ]

    def allocations_for_line(self, line_id: int) -> list[dict]:
        return [
            dict(r)
            for r in self.conn.execute(
                """SELECT a.*, u.seq unit_seq FROM allocations a
                   JOIN resource_units u ON u.id=a.unit_id
                   WHERE a.line_id=? ORDER BY u.seq""",
                (line_id,),
            ).fetchall()
        ]

    def readiness_blockers(self, request_id: int) -> list[dict]:
        """逐项检查开工就绪状态；返回 blocker 列表（空列表=全部就绪）。"""
        blockers: list[dict] = []
        for line in self.list_lines(request_id):
            if line["status"] != "HELD":
                continue
            resource = self.get_resource_row(line["resource_code"])
            if resource is None or not resource["online"]:
                blockers.append({
                    "line_id": line["id"],
                    "resource_code": line["resource_code"],
                    "reason": "RESOURCE_OFFLINE",
                    "message": f"资源 {line['resource_code']} 已维修下线",
                    "unit_id": None,
                })
                continue
            for alloc in self.allocations_for_line(line["id"]):
                unit = self.get_unit(alloc["unit_id"])
                if unit is not None and unit["status"] == "MAINTENANCE":
                    blockers.append({
                        "line_id": line["id"],
                        "resource_code": line["resource_code"],
                        "reason": "UNIT_MAINTENANCE",
                        "message": f"实例 #{alloc['unit_id']} 处于维修/失效状态",
                        "unit_id": alloc["unit_id"],
                    })
        return blockers

    def replace_allocation(self, line_id: int, old_unit_id: int, new_unit_id: int,
                           start: str, end: str) -> None:
        """同事务内删除旧承诺、建立新承诺；触发器校验新实例可用。"""
        self.conn.execute(
            "DELETE FROM allocations WHERE line_id=? AND unit_id=?",
            (line_id, old_unit_id),
        )
        line = self.get_line(line_id)
        self.insert_allocation(line_id, new_unit_id, line["resource_code"], start, end)

    # ---------------- 审计 / 幂等 ----------------
    def insert_audit(self, action, entity_type, entity_id, detail, ts,
                     plan_id=None, status="SUCCESS") -> None:
        from .errors import dumps
        self.conn.execute(
            """INSERT INTO audit_log(action,entity_type,entity_id,plan_id,status,detail_json,created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (action, entity_type, str(entity_id), plan_id, status, dumps(detail), ts),
        )

    def list_audit(self, limit: int = 100, offset: int = 0, entity_type=None,
                   entity_id=None) -> list[dict]:
        sql = "SELECT * FROM audit_log WHERE 1=1"
        args: list = []
        if entity_type:
            sql += " AND entity_type=?"
            args.append(entity_type)
        if entity_id is not None:
            sql += " AND entity_id=?"
            args.append(str(entity_id))
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        args.extend([limit, offset])
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def get_idempotency(self, key: str):
        return self.conn.execute(
            "SELECT * FROM idempotency_keys WHERE idempotency_key=?", (key,)
        ).fetchone()

    def save_idempotency(self, key, method, path, request_hash, code, body, ts) -> None:
        from .errors import dumps
        self.conn.execute(
            """INSERT INTO idempotency_keys
               (idempotency_key,method,path,request_hash,response_code,response_body,created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (key, method, path, request_hash, code, dumps(body), ts),
        )
