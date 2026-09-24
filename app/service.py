"""业务编排：区段互斥（既有）+ 资源预约、就绪阻断、替换、释放、审计（新增）。"""
from __future__ import annotations

import hashlib
import sqlite3

from .db import transaction
from .errors import ApiError, dumps
from .sections import known_section, plans_conflict
from .store import Store
from .timeutil import iso, now_iso, parse_iso


class Service:
    def __init__(self, conn):
        self.conn = conn
        self.store = Store(conn)
        import threading
        self._local = threading.local()

    # ---------------- 计划（含既有区段互斥规则） ----------------
    def create_plan(self, body: dict) -> dict:
        from .errors import require
        name = require(body, "name", str)
        section = require(body, "section", str)
        if not known_section(section):
            raise ApiError("UNKNOWN_SECTION", f"未知区段: {section}", 400)
        start = iso(require(body, "window_start", str), "window_start")
        end = iso(require(body, "window_end", str), "window_end")
        if not start < end:
            raise ApiError("VALIDATION_ERROR", "window_start 必须早于 window_end", 400)

        ts = now_iso()
        # 读阶段：既有规则检查（与任何在执行/待执行计划窗口重叠且区段互斥 -> 拒绝）
        candidate = {"section": section, "window_start": start, "window_end": end}
        conflicts = [
            {"plan_id": other["id"], "section": other["section"]}
            for other in self.store.active_plans_except(None)
            if plans_conflict(candidate, other)
        ]
        with transaction(self.conn):
            if conflicts:
                self.store.insert_audit(
                    "PLAN_REJECTED", "plan", 0,
                    {"reason": "SECTION_MUTEX_CONFLICT", "conflicts": conflicts}, ts,
                    status="DENIED",
                )
                raise ApiError(
                    "SECTION_MUTEX_CONFLICT",
                    f"区段 {section} 在该窗口与既有计划互斥",
                    409,
                    details=conflicts,
                )
            plan_id = self.store.create_plan(name, section, start, end, ts)
            self.store.insert_audit("PLAN_CREATED", "plan", plan_id,
                                    {"name": name, "section": section,
                                     "window": [start, end]}, ts)
        plan = self.store.get_plan(plan_id)
        plan["reservation"] = None
        return plan

    def get_plan_detail(self, plan_id: int) -> dict:
        plan = self.store.get_plan(plan_id)
        request = self.store.get_request_by_plan(plan_id)
        plan["reservation"] = self._reservation_view(request) if request else None
        return plan

    # ---------------- 预约 ----------------
    def reserve(self, plan_id: int, body: dict, idem: tuple | None = None):
        """预约资源。返回 (body, http_status, replayed)。

        idem = (key, method, path, raw_body_text) 时启用幂等：
        同键重试直接返回首次结果，绝不重复占量。
        """
        ts = now_iso()
        with transaction(self.conn):
            if idem is not None:
                replay = self._idempotency_lookup(idem)
                if replay is not None:
                    return replay
            plan = self.store.get_plan(plan_id)
            if plan["status"] not in ("SCHEDULED", "IN_PROGRESS"):
                raise ApiError("PLAN_NOT_OPEN",
                               f"计划状态 {plan['status']} 不可预约", 409)
            if self.store.get_request_by_plan(plan_id) is not None:
                # 重复预约（无幂等键的再次调用）：明确拒绝而不是叠加占量
                raise ApiError("PLAN_ALREADY_RESERVED",
                               "该计划已有预约单，请勿重复预约", 409)

            items = self._validate_items(body, plan)
            request_id = self.store.create_request(plan_id, ts)
            result_items = []
            try:
                for code, quantity, w_start, w_end in items:
                    self._check_resource(code, quantity)
                    line_id = self.store.create_line(
                        request_id, code, quantity, w_start, w_end, ts
                    )
                    try:
                        unit_ids = self._allocate_line(line_id, code, quantity, w_start, w_end)
                    except ApiError as exc:
                        if exc.code == "RESOURCE_EXHAUSTED":
                            self._deny_in_tx(plan_id, code, quantity, exc)
                        raise
                    except sqlite3.Error as exc:
                        # 触发器兜底（并发竞争下的重叠/失效）：回滚并补记拒绝审计
                        self._deny_in_tx(plan_id, code, quantity, exc)
                        raise self._sqlite_to_api(code, exc) from exc
                    result_items.append({
                        "line_id": line_id,
                        "resource_code": code,
                        "quantity": quantity,
                        "window_start": w_start,
                        "window_end": w_end,
                        "unit_ids": unit_ids,
                    })
            except ApiError:
                # _deny_in_tx 已在独立事务中落 DENIED 审计；此处保持无活动事务现场
                if self.conn.in_transaction:
                    self.conn.execute("ROLLBACK")
                raise

            view = self._reservation_view(self.store.get_request(request_id))
            view["items"] = result_items
            result = {"reservation": view, "replayed": False}
            if idem is not None:
                self._save_idempotency(idem, 201, result, ts)
            self.store.insert_audit(
                "RESERVED", "plan", plan_id,
                {"request_id": request_id, "items": [
                    {"resource_code": i[0], "quantity": i[1],
                     "window": [i[2], i[3]]} for i in items]},
                ts, plan_id=plan_id,
            )
        return result, 201, False

    def _sqlite_to_api(self, code: str, exc: sqlite3.Error) -> ApiError:
        message = str(exc)
        if message.startswith(("ALLOC_OVERLAP", "UNIT_NOT_AVAILABLE", "RESOURCE_OFFLINE")):
            return ApiError("RESOURCE_EXHAUSTED",
                            f"资源 {code} 预约失败（并发竞争或实例失效）：{message}", 409)
        return ApiError("PERSISTENCE_ERROR", message, 409)

    def _deny_in_tx(self, plan_id, code, quantity, exc) -> None:
        """回滚全部占用，在独立事务中补记一条 DENIED 审计；
        调用方随后抛出业务异常（外层 contextmanager 见无活动事务即不再动作）。"""
        self.conn.execute("ROLLBACK")
        with transaction(self.conn):
            self.store.insert_audit(
                "RESERVE_DENIED", "plan", plan_id,
                {"resource_code": code, "quantity": quantity, "reason": str(exc)},
                now_iso(), plan_id=plan_id, status="DENIED",
            )

    def _validate_items(self, body: dict, plan: dict) -> list[tuple]:
        if not isinstance(body, dict) or not isinstance(body.get("items"), list) or not body["items"]:
            raise ApiError("VALIDATION_ERROR", "items 必须是非空数组", 400)
        seen: set[str] = set()
        normalized = []
        for idx, raw in enumerate(body["items"]):
            if not isinstance(raw, dict):
                raise ApiError("VALIDATION_ERROR", f"items[{idx}] 必须是对象", 400)
            code = raw.get("resource_code")
            qty = raw.get("quantity")
            if not isinstance(code, str):
                raise ApiError("VALIDATION_ERROR",
                               f"items[{idx}].resource_code 必须是字符串", 400)
            if code in seen:
                raise ApiError("VALIDATION_ERROR",
                               f"资源 {code} 在同一预约中重复声明", 400)
            seen.add(code)
            if not isinstance(qty, int) or isinstance(qty, bool) or qty <= 0:
                raise ApiError("VALIDATION_ERROR",
                               f"items[{idx}].quantity 必须是正整数", 400)
            try:
                w_start = iso(raw["window_start"], f"items[{idx}].window_start") \
                    if raw.get("window_start") else None
                w_end = iso(raw["window_end"], f"items[{idx}].window_end") \
                    if raw.get("window_end") else None
            except ValueError as exc:
                raise ApiError("VALIDATION_ERROR", str(exc), 400) from exc
            if (w_start is None) != (w_end is None):
                raise ApiError("VALIDATION_ERROR",
                               f"items[{idx}] 的占用窗口必须同时提供起止", 400)
            if w_start and not w_start < w_end:
                raise ApiError("VALIDATION_ERROR",
                               f"items[{idx}] 占用窗口起止非法", 400)
            normalized.append((code, qty,
                               w_start or plan["window_start"],
                               w_end or plan["window_end"]))
        return normalized

    def _check_resource(self, code: str, quantity: int) -> None:
        row = self.store.get_resource_row(code)
        if row is None:
            raise ApiError("RESOURCE_NOT_FOUND", f"资源不存在: {code}", 404)
        if not row["online"]:
            raise ApiError("RESOURCE_OFFLINE", f"资源 {code} 已维修下线，无法预约", 409)
        total = self.conn.execute(
            "SELECT COUNT(*) n FROM resource_units WHERE resource_code=? AND status='AVAILABLE'",
            (code,),
        ).fetchone()["n"]
        if quantity > total:
            raise ApiError("RESOURCE_EXHAUSTED",
                           f"资源 {code} 存量不足：需要 {quantity}，可用上限 {total}", 409,
                           details=[{"resource_code": code, "requested": quantity,
                                     "available": total}])

    def _allocate_line(self, line_id, code, quantity, start, end,
                       exclude_unit_ids=()) -> list[int]:
        unit_ids = self.store.find_bookable_units(code, start, end, exclude_unit_ids)
        if len(unit_ids) < quantity:
            raise ApiError(
                "RESOURCE_EXHAUSTED",
                f"资源 {code} 在窗口 [{start}, {end}) 库存不足："
                f"需要 {quantity}，可订 {len(unit_ids)}",
                409,
                details=[{"resource_code": code, "requested": quantity,
                          "available": len(unit_ids),
                          "window_start": start, "window_end": end}],
            )
        chosen = unit_ids[:quantity]
        for unit_id in chosen:
            self.store.insert_allocation(line_id, unit_id, code, start, end)
        return chosen

    # ---------------- 就绪检查 / 开工 / 释放 ----------------
    def readiness(self, plan_id: int) -> dict:
        plan, request = self._require_reservation(plan_id)
        blockers = self.store.readiness_blockers(request["id"])
        return {
            "plan_id": plan_id,
            "ready": not blockers,
            "blockers": blockers,
            "reservation": self._reservation_view(request),
        }

    def start(self, plan_id: int) -> dict:
        self._require_reservation(plan_id)
        ts = now_iso()
        with transaction(self.conn):
            _, request = self._require_reservation(plan_id)
            blockers = self.store.readiness_blockers(request["id"])
            if blockers:
                # 开工前资源失效：逐项阻断；保留 DENIED 审计（先提交审计再报错）
                self.store.insert_audit(
                    "START_BLOCKED", "plan", plan_id,
                    {"blockers": blockers}, ts, plan_id=plan_id, status="DENIED",
                )
                self.conn.execute("COMMIT")
                raise ApiError("RESOURCE_NOT_READY",
                               "开工被阻断：存在失效/下线资源，请逐项替换后重试",
                               409, details=blockers)
            self.store.set_plan_status(plan_id, "IN_PROGRESS")
            self.store.insert_audit("PLAN_STARTED", "plan", plan_id, {}, ts, plan_id=plan_id)
        return {"plan_id": plan_id, "status": "IN_PROGRESS", "ready": True}

    def release(self, plan_id: int) -> dict:
        self._require_reservation(plan_id)
        ts = now_iso()
        with transaction(self.conn):
            # BEGIN IMMEDIATE 已将并发销记排队；条件更新保证只生效一次
            request = self.store.get_request_by_plan(plan_id)
            cur = self.conn.execute(
                "UPDATE reservation_requests SET status='RELEASED', released_at=? "
                "WHERE id=? AND status='CONFIRMED'",
                (ts, request["id"]),
            )
            if cur.rowcount == 0:
                # 此前已销记：返回首次销记时间，不重复释放、不改库存
                prior = self.store.get_request(request["id"])
                return {"plan_id": plan_id, "released": False, "already_released": True,
                        "released_at": prior["released_at"]}
            self.conn.execute(
                "UPDATE reservation_lines SET status='RELEASED' "
                "WHERE request_id=? AND status='HELD'",
                (request["id"],),
            )
            self.store.set_plan_status(plan_id, "COMPLETED")
            self.store.insert_audit("RELEASED", "plan", plan_id,
                                    {"request_id": request["id"]}, ts, plan_id=plan_id)
        return {"plan_id": plan_id, "released": True, "already_released": False,
                "released_at": ts}

    # ---------------- 替换（阻断后恢复） ----------------
    def replace_unit(self, plan_id: int, body: dict) -> dict:
        """替换失效占用：
        - 同资源换机：{line_id, old_unit_id, new_unit_id}
        - 替代资源：{line_id, replacement_resource_code, quantity?}
        """
        plan, request = self._require_reservation(plan_id)
        if request["status"] != "CONFIRMED":
            raise ApiError("REQUEST_NOT_OPEN", "预约已销记，无法替换", 409)
        line_id = body.get("line_id")
        line = self.store.get_line(line_id) if isinstance(line_id, int) else None
        if line is None or line["request_id"] != request["id"]:
            raise ApiError("LINE_NOT_FOUND", "预约明细不存在或不属于该计划", 404)
        if line["status"] != "HELD":
            raise ApiError("LINE_NOT_HELD", f"明细行状态为 {line['status']}，不可替换", 409)

        ts = now_iso()
        with transaction(self.conn):
            if "replacement_resource_code" in body:
                result = self._replace_with_resource(plan_id, line,
                                                     body["replacement_resource_code"],
                                                     body.get("quantity"), ts)
            else:
                result = self._replace_with_unit(plan_id, line, body, ts)
            self.store.insert_audit("ALLOCATION_REPLACED", "plan", plan_id, result, ts,
                                    plan_id=plan_id)
            blockers = self.store.readiness_blockers(request["id"])
        return {"replacement": result, "ready": not blockers, "blockers": blockers}

    def _replace_with_unit(self, plan_id, line, body, ts) -> dict:
        old_unit_id = body.get("old_unit_id")
        new_unit_id = body.get("new_unit_id")
        if not isinstance(old_unit_id, int) or not isinstance(new_unit_id, int):
            raise ApiError("VALIDATION_ERROR",
                           "old_unit_id 与 new_unit_id 必须为整数", 400)
        owned = {a["unit_id"] for a in self.store.allocations_for_line(line["id"])}
        if old_unit_id not in owned:
            raise ApiError("UNIT_NOT_ALLOCATED",
                           f"实例 #{old_unit_id} 不在该明细行占用中", 409)
        if new_unit_id in owned:
            raise ApiError("UNIT_ALREADY_ALLOCATED",
                           f"实例 #{new_unit_id} 已被本行占用", 409)
        new_unit = self.store.get_unit(new_unit_id)
        if new_unit is None or new_unit["resource_code"] != line["resource_code"]:
            raise ApiError("UNIT_MISMATCH",
                           f"替换实例必须属于资源 {line['resource_code']}", 409)
        # 同事务先删旧再插新；触发器校验新实例窗口可用
        try:
            self.store.replace_allocation(
                line["id"], old_unit_id, new_unit_id,
                line["window_start"], line["window_end"],
            )
        except sqlite3.Error as exc:
            self._deny_in_tx(self._replace_plan_id, line["resource_code"], 1, exc)
            raise self._sqlite_to_api(line["resource_code"], exc) from exc
        return {"type": "UNIT", "line_id": line["id"],
                "old_unit_id": old_unit_id, "new_unit_id": new_unit_id,
                "resource_code": line["resource_code"]}

    def _replace_with_resource(self, plan_id, line, alt_code, quantity, ts) -> dict:
        if not isinstance(alt_code, str):
            raise ApiError("VALIDATION_ERROR", "replacement_resource_code 必须是字符串", 400)
        qty = quantity if isinstance(quantity, int) and not isinstance(quantity, bool) \
            and quantity > 0 else line["quantity"]
        self._check_resource(alt_code, qty)
        new_line_id = self.store.create_line(
            line["request_id"], alt_code, qty,
            line["window_start"], line["window_end"], ts,
            replaces_line_id=line["id"],
        )
        try:
            unit_ids = self._allocate_line(new_line_id, alt_code, qty,
                                           line["window_start"], line["window_end"])
        except sqlite3.Error as exc:
            self._deny_in_tx(plan_id, alt_code, qty, exc)
            raise self._sqlite_to_api(alt_code, exc) from exc
        self.store.mark_line_replaced(line["id"])
        return {"type": "RESOURCE", "old_line_id": line["id"],
                "new_line_id": new_line_id,
                "old_resource_code": line["resource_code"],
                "replacement_resource_code": alt_code,
                "quantity": qty, "unit_ids": unit_ids}

    # ---------------- 资源运维 ----------------
    def set_resource_online(self, code: str, online: bool) -> dict:
        ts = now_iso()
        with transaction(self.conn):
            self.store.set_resource_online(code, online, ts)
            self.store.insert_audit(
                "RESOURCE_ONLINE" if online else "RESOURCE_OFFLINE",
                "resource", code, {"online": online}, ts,
            )
        return self.store.get_resource(code)

    def set_unit_maintenance(self, unit_id: int, maintenance: bool) -> dict:
        ts = now_iso()
        unit = self.store.get_unit(unit_id)
        if unit is None:
            raise ApiError("UNIT_NOT_FOUND", f"资源实例不存在: {unit_id}", 404)
        with transaction(self.conn):
            self.store.set_unit_status(unit_id, "MAINTENANCE" if maintenance else "AVAILABLE", ts)
            self.store.insert_audit(
                "UNIT_MAINTENANCE" if maintenance else "UNIT_AVAILABLE",
                "resource_unit", unit_id,
                {"resource_code": unit["resource_code"], "maintenance": maintenance}, ts,
            )
        return self.store._unit_to_dict(self.store.get_unit(unit_id))

    # ---------------- 视图 / 幂等 ----------------
    def _reservation_view(self, request) -> dict:
        lines = []
        for line in self.store.list_lines(request["id"]):
            units = [a["unit_id"] for a in self.store.allocations_for_line(line["id"])]
            lines.append({
                "line_id": line["id"],
                "resource_code": line["resource_code"],
                "quantity": line["quantity"],
                "window_start": line["window_start"],
                "window_end": line["window_end"],
                "status": line["status"],
                "replaces_line_id": line["replaces_line_id"],
                "unit_ids": units,
            })
        return {
            "request_id": request["id"],
            "plan_id": request["plan_id"],
            "status": request["status"],
            "created_at": request["created_at"],
            "released_at": request["released_at"],
            "lines": lines,
        }

    def _require_reservation(self, plan_id: int):
        plan = self.store.get_plan(plan_id)
        request = self.store.get_request_by_plan(plan_id)
        if request is None:
            raise ApiError("RESERVATION_NOT_FOUND",
                           f"计划 {plan_id} 尚无预约单", 404)
        return plan, request

    def _idempotency_lookup(self, idem):
        key, method, path, raw = idem
        request_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        row = self.store.get_idempotency(key)
        if row is None:
            self._local.idem_pending = (key, method, path, request_hash)
            return None
        import json
        if row["request_hash"] != request_hash:
            raise ApiError("IDEMPOTENCY_CONFLICT",
                           "幂等键被不同请求体复用", 409)
        body = json.loads(row["response_body"])
        body["replayed"] = True
        return body, row["response_code"], True

    def _save_idempotency(self, idem, code, body, ts) -> None:
        pending = getattr(self._local, "idem_pending", None)
        if pending is None:  # 理论上不会发生（lookup 已先执行）
            key, method, path, raw = idem
            pending = (key, method, path, hashlib.sha256(raw.encode("utf-8")).hexdigest())
        self.store.save_idempotency(*pending, code, body, ts)
        self._local.idem_pending = None
