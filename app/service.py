"""Business logic for locally simulated protective-material reservation."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from .db import canonical_pair, connect, initialize
from .errors import BusinessError

UNIT_STATUSES = {"available", "maintenance", "retired"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_time(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BusinessError(
            "invalid_request", f"{field} must be an ISO-8601 timestamp", status_code=400
        )
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BusinessError(
            "invalid_request", f"{field} must be an ISO-8601 timestamp", status_code=400
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def require_string(payload: dict[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise BusinessError("invalid_request", f"{field} is required", status_code=400)
    return value.strip()


def request_signature(
    segment: str,
    start_at: str,
    end_at: str,
    requirements: list[dict[str, Any]],
) -> str:
    normalized = {
        "segment": segment,
        "start_at": start_at,
        "end_at": end_at,
        "requirements": sorted(
            ({"sku": item["sku"], "quantity": item["quantity"]} for item in requirements),
            key=lambda item: item["sku"],
        ),
    }
    return hashlib.sha256(
        json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


class ReservationService:
    """All writes are transaction-scoped; every business mutation emits audit."""

    def __init__(self, database: str = ":memory:") -> None:
        if database == ":memory:":
            # Shared-cache memory DB lets worker threads see the same database.
            database = f"file:reservation-{uuid.uuid4().hex}?mode=memory&cache=shared"
        self.database = database
        self._local = threading.local()
        self._connections: set[sqlite3.Connection] = set()
        self._connections_lock = threading.Lock()
        # Keep this connection alive so a shared in-memory DB is not dropped.
        self._keeper = self._new_connection()
        initialize(self._keeper)

    def _new_connection(self) -> sqlite3.Connection:
        conn = connect(self.database)
        with self._connections_lock:
            self._connections.add(conn)
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._new_connection()
            self._local.conn = conn
        return conn

    def close(self) -> None:
        with self._connections_lock:
            connections = list(self._connections)
            self._connections.clear()
        for conn in connections:
            conn.close()

    def _begin(self) -> None:
        self.conn.execute("BEGIN IMMEDIATE")

    def _commit(self) -> None:
        self.conn.execute("COMMIT")

    def _rollback(self) -> None:
        self.conn.execute("ROLLBACK")

    def _audit(
        self,
        conn: sqlite3.Connection,
        event_type: str,
        *,
        plan_id: Optional[str] = None,
        request_id: Optional[str] = None,
        detail: Optional[dict[str, Any]] = None,
    ) -> None:
        conn.execute(
            """INSERT INTO audit_log(event_id, event_type, plan_id, request_id, detail_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                new_id("evt"),
                event_type,
                plan_id,
                request_id,
                json.dumps(detail or {}, sort_keys=True),
                utc_now(),
            ),
        )

    def _audit_outside(
        self,
        event_type: str,
        *,
        plan_id: Optional[str] = None,
        request_id: Optional[str] = None,
        detail: Optional[dict[str, Any]] = None,
    ) -> None:
        """Persist failure/validation-visible audit after a rolled-back write."""

        self._begin()
        try:
            self._audit(
                self.conn,
                event_type,
                plan_id=plan_id,
                request_id=request_id,
                detail=detail,
            )
            self._commit()
        except Exception:
            self._rollback()
            raise

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {key: row[key] for key in row.keys()}

    def _get_unit(self, conn: sqlite3.Connection, unit_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT sku, unit_id, base_status, created_at FROM resource_units WHERE unit_id = ?",
            (unit_id,),
        ).fetchone()
        if row is None:
            raise BusinessError("resource_not_found", f"resource unit {unit_id} does not exist", status_code=404)
        return row

    def _get_plan(self, conn: sqlite3.Connection, plan_id: str) -> sqlite3.Row:
        row = conn.execute(
            """SELECT plan_id, segment, start_at, end_at, status, idempotency_key,
                      request_hash, created_at, released_at
                 FROM plans WHERE plan_id = ?""",
            (plan_id,),
        ).fetchone()
        if row is None:
            raise BusinessError("plan_not_found", f"plan {plan_id} does not exist", status_code=404)
        return row

    def _unit_has_active_hold_now(
        self, conn: sqlite3.Connection, unit_id: str, now: str
    ) -> bool:
        row = conn.execute(
            """SELECT 1
                 FROM reservation_items ri
                 JOIN plans p ON p.plan_id = ri.plan_id
                WHERE ri.unit_id = ?
                  AND ri.active = 1
                  AND p.status <> 'released'
                  AND ri.start_at <= ?
                  AND ? < ri.end_at
                LIMIT 1""",
            (unit_id, now, now),
        ).fetchone()
        return row is not None

    def _unit_effective_status(
        self, conn: sqlite3.Connection, unit: sqlite3.Row, now: Optional[str] = None
    ) -> str:
        if unit["base_status"] != "available":
            return unit["base_status"]
        now = now or utc_now()
        if self._unit_has_active_hold_now(conn, unit["unit_id"], now):
            return "occupied"
        return "available"

    def _unit_payload(self, conn: sqlite3.Connection, unit: sqlite3.Row) -> dict[str, Any]:
        return {
            "sku": unit["sku"],
            "unit_id": unit["unit_id"],
            "base_status": unit["base_status"],
            "effective_status": self._unit_effective_status(conn, unit),
            "created_at": unit["created_at"],
        }

    def _active_items(self, conn: sqlite3.Connection, plan_id: str) -> list[sqlite3.Row]:
        return conn.execute(
            """SELECT ri.plan_id, ri.sku, ri.unit_id, ri.start_at, ri.end_at, ri.active,
                      ru.base_status
                 FROM reservation_items ri
                 JOIN resource_units ru ON ru.unit_id = ri.unit_id
                WHERE ri.plan_id = ? AND ri.active = 1
                ORDER BY ri.sku, ri.unit_id""",
            (plan_id,),
        ).fetchall()

    def _plan_payload(self, conn: sqlite3.Connection, plan: sqlite3.Row) -> dict[str, Any]:
        requirements = conn.execute(
            "SELECT sku, quantity FROM plan_requirements WHERE plan_id = ? ORDER BY sku",
            (plan["plan_id"],),
        ).fetchall()
        items = self._active_items(conn, plan["plan_id"])
        return {
            "plan_id": plan["plan_id"],
            "segment": plan["segment"],
            "start_at": plan["start_at"],
            "end_at": plan["end_at"],
            "status": plan["status"],
            "idempotency_key": plan["idempotency_key"],
            "created_at": plan["created_at"],
            "released_at": plan["released_at"],
            "requirements": [self._row_to_dict(row) for row in requirements],
            "allocations": [
                {
                    "sku": item["sku"],
                    "unit_id": item["unit_id"],
                    "unit_base_status": item["base_status"],
                    "start_at": item["start_at"],
                    "end_at": item["end_at"],
                }
                for item in items
            ],
        }

    # ------------------------------------------------------------------
    # Segment mutex matrix (existing rules are read, never changed)
    # ------------------------------------------------------------------
    def list_segment_rules(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT segment_a, segment_b, can_parallel FROM segment_rules ORDER BY segment_a, segment_b"
        ).fetchall()
        return [
            {"segment_a": row["segment_a"], "segment_b": row["segment_b"], "can_parallel": bool(row["can_parallel"])}
            for row in rows
        ]

    def _segment_can_parallel(
        self, conn: sqlite3.Connection, segment_a: str, segment_b: str
    ) -> bool:
        a, b = canonical_pair(segment_a, segment_b)
        row = conn.execute(
            "SELECT can_parallel FROM segment_rules WHERE segment_a = ? AND segment_b = ?",
            (a, b),
        ).fetchone()
        # Existing local matrix defaults to allowing pairs it does not forbid.
        return True if row is None else bool(row["can_parallel"])

    # ------------------------------------------------------------------
    # Resource inventory
    # ------------------------------------------------------------------
    def create_resource(self, payload: dict[str, Any]) -> dict[str, Any]:
        sku = require_string(payload, "sku")
        name = require_string(payload, "name")
        quantity = payload.get("quantity", 1)
        if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0:
            raise BusinessError("invalid_request", "quantity must be a positive integer", status_code=400)
        now = utc_now()
        self._begin()
        try:
            existing = self.conn.execute(
                "SELECT sku FROM resource_skus WHERE sku = ?", (sku,)
            ).fetchone()
            if existing is not None:
                raise BusinessError("resource_sku_exists", f"resource sku {sku} already exists", status_code=409)
            self.conn.execute(
                "INSERT INTO resource_skus(sku, name, created_at) VALUES (?, ?, ?)",
                (sku, name, now),
            )
            unit_ids = []
            for index in range(1, quantity + 1):
                unit_id = f"{sku}-{index:03d}"
                self.conn.execute(
                    "INSERT INTO resource_units(sku, unit_id, base_status, created_at) VALUES (?, ?, 'available', ?)",
                    (sku, unit_id, now),
                )
                unit_ids.append(unit_id)
            self._audit(
                self.conn,
                "resource_created",
                detail={"sku": sku, "name": name, "quantity": quantity, "unit_ids": unit_ids},
            )
            self._commit()
        except Exception:
            self._rollback()
            raise
        return {"sku": sku, "name": name, "quantity": quantity, "unit_ids": unit_ids, "created_at": now}

    def expand_resource(self, sku: str, payload: dict[str, Any]) -> dict[str, Any]:
        quantity = payload.get("quantity")
        if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0:
            raise BusinessError("invalid_request", "quantity must be a positive integer", status_code=400)
        now = utc_now()
        self._begin()
        try:
            sku_row = self.conn.execute(
                "SELECT sku, name FROM resource_skus WHERE sku = ?", (sku,)
            ).fetchone()
            if sku_row is None:
                raise BusinessError("resource_not_found", f"resource sku {sku} does not exist", status_code=404)
            count_row = self.conn.execute(
                "SELECT COUNT(*) AS c FROM resource_units WHERE sku = ?",
                (sku,),
            ).fetchone()
            start = int(count_row["c"]) + 1
            unit_ids = []
            for index in range(start, start + quantity):
                unit_id = f"{sku}-{index:03d}"
                self.conn.execute(
                    "INSERT INTO resource_units(sku, unit_id, base_status, created_at) VALUES (?, ?, 'available', ?)",
                    (sku, unit_id, now),
                )
                unit_ids.append(unit_id)
            self._audit(
                self.conn,
                "resource_expanded",
                detail={"sku": sku, "quantity": quantity, "unit_ids": unit_ids},
            )
            self._commit()
        except Exception:
            self._rollback()
            raise
        return {"sku": sku, "added_quantity": quantity, "unit_ids": unit_ids}

    def list_resources(self) -> list[dict[str, Any]]:
        now = utc_now()
        rows = self.conn.execute(
            "SELECT sku, name, created_at FROM resource_skus ORDER BY sku"
        ).fetchall()
        result = []
        for row in rows:
            units = self.conn.execute(
                "SELECT sku, unit_id, base_status, created_at FROM resource_units WHERE sku = ? ORDER BY unit_id",
                (row["sku"],),
            ).fetchall()
            counts = {"available": 0, "occupied": 0, "maintenance": 0, "retired": 0}
            for unit in units:
                counts[self._unit_effective_status(self.conn, unit, now)] += 1
            result.append(
                {
                    "sku": row["sku"],
                    "name": row["name"],
                    "created_at": row["created_at"],
                    "total_units": len(units),
                    "status_counts": counts,
                }
            )
        return result

    def list_units(self, sku: str) -> list[dict[str, Any]]:
        sku_row = self.conn.execute(
            "SELECT sku FROM resource_skus WHERE sku = ?", (sku,)
        ).fetchone()
        if sku_row is None:
            raise BusinessError("resource_not_found", f"resource sku {sku} does not exist", status_code=404)
        rows = self.conn.execute(
            "SELECT sku, unit_id, base_status, created_at FROM resource_units WHERE sku = ? ORDER BY unit_id",
            (sku,),
        ).fetchall()
        return [self._unit_payload(self.conn, row) for row in rows]

    def set_unit_status(self, unit_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        status = require_string(payload, "status")
        if status not in UNIT_STATUSES:
            raise BusinessError(
                "invalid_request",
                "status must be one of available, maintenance, retired",
                status_code=400,
            )
        reason = payload.get("reason")
        if reason is not None and not isinstance(reason, str):
            raise BusinessError("invalid_request", "reason must be a string", status_code=400)
        now = utc_now()
        self._begin()
        try:
            unit = self._get_unit(self.conn, unit_id)
            old_status = unit["base_status"]
            if old_status == status:
                self._commit()
                return self._unit_payload(self.conn, unit)
            if old_status == "retired":
                raise BusinessError(
                    "invalid_transition",
                    "a retired resource cannot change state",
                    status_code=409,
                )
            if status == "maintenance":
                active_plan = self.conn.execute(
                    """SELECT p.plan_id
                         FROM reservation_items ri
                         JOIN plans p ON p.plan_id = ri.plan_id
                        WHERE ri.unit_id = ?
                          AND ri.active = 1
                          AND p.status = 'in_progress'
                          AND ri.start_at <= ?
                          AND ? < ri.end_at
                        LIMIT 1""",
                    (unit_id, now, now),
                ).fetchone()
                if active_plan is not None:
                    raise BusinessError(
                        "resource_in_use",
                        "resource is in use by an in-progress plan and cannot enter maintenance",
                        status_code=409,
                        details={"plan_id": active_plan["plan_id"]},
                    )
            self.conn.execute(
                "UPDATE resource_units SET base_status = ? WHERE unit_id = ?",
                (status, unit_id),
            )
            self._audit(
                self.conn,
                "resource_status_changed",
                detail={
                    "unit_id": unit_id,
                    "sku": unit["sku"],
                    "old_status": old_status,
                    "new_status": status,
                    "reason": reason,
                },
            )
            updated = self._get_unit(self.conn, unit_id)
            self._commit()
        except Exception:
            self._rollback()
            raise
        return self._unit_payload(self.conn, updated)

    # ------------------------------------------------------------------
    # Reservation
    # ------------------------------------------------------------------
    def _normalize_requirements(self, raw: Any) -> list[dict[str, Any]]:
        if not isinstance(raw, list) or not raw:
            raise BusinessError("invalid_request", "requirements must be a non-empty array", status_code=400)
        merged: dict[str, int] = {}
        for item in raw:
            if not isinstance(item, dict):
                raise BusinessError("invalid_request", "each requirement must be an object", status_code=400)
            sku = item.get("sku")
            quantity = item.get("quantity")
            if not isinstance(sku, str) or not sku.strip():
                raise BusinessError("invalid_request", "requirement sku is required", status_code=400)
            if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0:
                raise BusinessError("invalid_request", "requirement quantity must be a positive integer", status_code=400)
            sku = sku.strip()
            merged[sku] = merged.get(sku, 0) + quantity
        return [{"sku": sku, "quantity": merged[sku]} for sku in sorted(merged)]

    def _candidate_units(
        self,
        conn: sqlite3.Connection,
        sku: str,
        start_at: str,
        end_at: str,
    ) -> list[str]:
        rows = conn.execute(
            """SELECT ru.unit_id
                 FROM resource_units ru
                WHERE ru.sku = ?
                  AND ru.base_status = 'available'
                  AND NOT EXISTS (
                        SELECT 1
                          FROM reservation_items ri
                          JOIN plans p ON p.plan_id = ri.plan_id
                         WHERE ri.unit_id = ru.unit_id
                           AND ri.active = 1
                           AND p.status <> 'released'
                           AND ri.start_at < ?
                           AND ? < ri.end_at
                  )
                ORDER BY ru.unit_id""",
            (sku, end_at, start_at),
        ).fetchall()
        return [row["unit_id"] for row in rows]

    def _segment_conflict(
        self, conn: sqlite3.Connection, segment: str, start_at: str, end_at: str
    ) -> Optional[dict[str, Any]]:
        rows = conn.execute(
            """SELECT plan_id, segment, start_at, end_at
                 FROM plans
                WHERE status <> 'released'
                  AND start_at < ?
                  AND ? < end_at
                ORDER BY plan_id""",
            (end_at, start_at),
        ).fetchall()
        for row in rows:
            if not self._segment_can_parallel(conn, segment, row["segment"]):
                return {
                    "plan_id": row["plan_id"],
                    "segment": row["segment"],
                    "start_at": row["start_at"],
                    "end_at": row["end_at"],
                }
        return None

    def _reservation_payload_from_row(
        self, conn: sqlite3.Connection, plan: sqlite3.Row, *, replayed: bool
    ) -> dict[str, Any]:
        payload = self._plan_payload(conn, plan)
        payload["replayed"] = replayed
        return payload

    def create_reservation(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if not isinstance(payload, dict):
            raise BusinessError("invalid_request", "request body must be an object", status_code=400)
        idempotency_key = require_string(payload, "idempotency_key")
        segment = require_string(payload, "segment")
        start_at = parse_time(payload.get("start_at"), "start_at")
        end_at = parse_time(payload.get("end_at"), "end_at")
        if not start_at < end_at:
            raise BusinessError("invalid_request", "start_at must be before end_at", status_code=400)
        requirements = self._normalize_requirements(payload.get("requirements"))
        signature = request_signature(segment, start_at, end_at, requirements)
        requested_plan_id = payload.get("plan_id")
        if requested_plan_id is not None and not isinstance(requested_plan_id, str):
            raise BusinessError("invalid_request", "plan_id must be a string", status_code=400)

        self._begin()
        try:
            existing = self.conn.execute(
                """SELECT plan_id, segment, start_at, end_at, status, idempotency_key,
                          request_hash, created_at, released_at
                     FROM plans WHERE idempotency_key = ?""",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if existing["request_hash"] != signature:
                    raise BusinessError(
                        "idempotency_conflict",
                        "idempotency_key was already used with a different request",
                        status_code=409,
                        details={"plan_id": existing["plan_id"]},
                    )
                body = self._reservation_payload_from_row(self.conn, existing, replayed=True)
                self._commit()
                return 200, body

            plan_id = (requested_plan_id or "").strip() or new_id("plan")
            existing_plan = self.conn.execute(
                "SELECT plan_id FROM plans WHERE plan_id = ?", (plan_id,)
            ).fetchone()
            if existing_plan is not None:
                raise BusinessError(
                    "plan_already_reserved",
                    f"plan {plan_id} already has a reservation",
                    status_code=409,
                )

            conflict = self._segment_conflict(self.conn, segment, start_at, end_at)
            if conflict is not None:
                raise BusinessError(
                    "segment_conflict",
                    "segment mutex rules do not allow this plan to run in parallel",
                    status_code=409,
                    details={"conflicting_plan": conflict},
                )

            shortages = []
            selected: dict[str, list[str]] = {}
            for requirement in requirements:
                candidates = self._candidate_units(self.conn, requirement["sku"], start_at, end_at)
                if len(candidates) < requirement["quantity"]:
                    shortages.append(
                        {
                            "sku": requirement["sku"],
                            "requested": requirement["quantity"],
                            "available": len(candidates),
                        }
                    )
                else:
                    selected[requirement["sku"]] = candidates[: requirement["quantity"]]
            if shortages:
                raise BusinessError(
                    "resource_unavailable",
                    "insufficient available resources for the requested window",
                    status_code=409,
                    details={"shortages": shortages},
                )

            now = utc_now()
            self.conn.execute(
                """INSERT INTO plans(plan_id, segment, start_at, end_at, status,
                                     idempotency_key, request_hash, created_at)
                   VALUES (?, ?, ?, ?, 'reserved', ?, ?, ?)""",
                (plan_id, segment, start_at, end_at, idempotency_key, signature, now),
            )
            for requirement in requirements:
                self.conn.execute(
                    "INSERT INTO plan_requirements(plan_id, sku, quantity) VALUES (?, ?, ?)",
                    (plan_id, requirement["sku"], requirement["quantity"]),
                )
                for unit_id in selected[requirement["sku"]]:
                    self.conn.execute(
                        """INSERT INTO reservation_items(plan_id, sku, unit_id, start_at, end_at, active)
                           VALUES (?, ?, ?, ?, ?, 1)""",
                        (plan_id, requirement["sku"], unit_id, start_at, end_at),
                    )
            self._audit(
                self.conn,
                "reservation_created",
                plan_id=plan_id,
                request_id=idempotency_key,
                detail={
                    "segment": segment,
                    "start_at": start_at,
                    "end_at": end_at,
                    "requirements": requirements,
                    "allocations": selected,
                },
            )
            plan = self._get_plan(self.conn, plan_id)
            body = self._reservation_payload_from_row(self.conn, plan, replayed=False)
            self._commit()
            return 201, body
        except BusinessError as exc:
            self._rollback()
            if exc.code in {"segment_conflict", "resource_unavailable"}:
                self._audit_outside(
                    "reservation_failed",
                    request_id=idempotency_key,
                    detail={
                        "code": exc.code,
                        "message": exc.message,
                        "segment": segment,
                        "start_at": start_at,
                        "end_at": end_at,
                        "requirements": requirements,
                        "details": exc.details,
                    },
                )
            raise
        except sqlite3.IntegrityError as exc:
            self._rollback()
            error = BusinessError(
                "reservation_conflict",
                "reservation conflicts with an existing allocation",
                status_code=409,
                details={"database_error": str(exc)},
            )
            self._audit_outside(
                "reservation_failed",
                request_id=idempotency_key,
                detail={
                    "code": error.code,
                    "message": error.message,
                    "segment": segment,
                    "start_at": start_at,
                    "end_at": end_at,
                    "requirements": requirements,
                    "details": error.details,
                },
            )
            raise error from exc
        except Exception:
            self._rollback()
            raise

    # ------------------------------------------------------------------
    # Pre-start gate and replacement
    # ------------------------------------------------------------------
    def readiness(self, plan_id: str) -> dict[str, Any]:
        plan = self._get_plan(self.conn, plan_id)
        items = self._active_items(self.conn, plan_id)
        blocked = []
        for item in items:
            if item["base_status"] != "available":
                blocked.append(
                    {
                        "sku": item["sku"],
                        "unit_id": item["unit_id"],
                        "base_status": item["base_status"],
                        "reason": "resource_unavailable_before_start",
                    }
                )
        return {
            "plan_id": plan_id,
            "status": plan["status"],
            "ready": not blocked,
            "blocked_items": blocked,
            "checked_at": utc_now(),
        }

    def start_plan(self, plan_id: str) -> tuple[int, dict[str, Any]]:
        now = utc_now()
        self._begin()
        try:
            plan = self._get_plan(self.conn, plan_id)
            if plan["status"] == "released":
                raise BusinessError("plan_released", "a released plan cannot be started", status_code=409)
            if plan["status"] == "in_progress":
                body = self._plan_payload(self.conn, plan)
                body["already_started"] = True
                self._commit()
                return 200, body
            if not (plan["start_at"] <= now < plan["end_at"]):
                raise BusinessError(
                    "outside_window",
                    "plan can only be started inside its reserved window",
                    status_code=409,
                    details={"start_at": plan["start_at"], "end_at": plan["end_at"], "now": now},
                )
            items = self._active_items(self.conn, plan_id)
            blocked = [
                {
                    "sku": item["sku"],
                    "unit_id": item["unit_id"],
                    "base_status": item["base_status"],
                    "reason": "resource_unavailable_before_start",
                }
                for item in items
                if item["base_status"] != "available"
            ]
            if blocked:
                raise BusinessError(
                    "prestart_blocked",
                    "one or more reserved resources are unavailable before start",
                    status_code=409,
                    details={"blocked_items": blocked},
                )
            self.conn.execute(
                "UPDATE plans SET status = 'in_progress' WHERE plan_id = ? AND status = 'reserved'",
                (plan_id,),
            )
            self._audit(
                self.conn,
                "plan_started",
                plan_id=plan_id,
                detail={"started_at": now},
            )
            updated = self._get_plan(self.conn, plan_id)
            body = self._plan_payload(self.conn, updated)
            body["already_started"] = False
            self._commit()
            return 200, body
        except BusinessError as exc:
            self._rollback()
            if exc.code == "prestart_blocked":
                self._audit_outside(
                    "plan_start_blocked",
                    plan_id=plan_id,
                    detail=exc.details,
                )
            raise
        except Exception:
            self._rollback()
            raise

    def replace_unit(self, plan_id: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        old_unit_id = require_string(payload, "old_unit_id")
        replacement_unit_id = payload.get("replacement_unit_id")
        if replacement_unit_id is not None and not isinstance(replacement_unit_id, str):
            raise BusinessError("invalid_request", "replacement_unit_id must be a string", status_code=400)
        reason = payload.get("reason") or "resource_unavailable_before_start"
        if not isinstance(reason, str):
            raise BusinessError("invalid_request", "reason must be a string", status_code=400)
        idempotency_key = payload.get("idempotency_key")
        if idempotency_key is not None and not isinstance(idempotency_key, str):
            raise BusinessError("invalid_request", "idempotency_key must be a string", status_code=400)

        self._begin()
        try:
            if idempotency_key:
                prior = self.conn.execute(
                    "SELECT * FROM replacements WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if prior is not None:
                    body = {
                        "plan_id": prior["plan_id"],
                        "old_unit_id": prior["old_unit_id"],
                        "replacement_unit_id": prior["replacement_unit_id"],
                        "sku": prior["sku"],
                        "reason": prior["reason"],
                        "replayed": True,
                    }
                    self._commit()
                    return 200, body

            plan = self._get_plan(self.conn, plan_id)
            if plan["status"] == "released":
                raise BusinessError("plan_released", "cannot replace resources for a released plan", status_code=409)
            item = self.conn.execute(
                """SELECT ri.plan_id, ri.sku, ri.unit_id, ri.start_at, ri.end_at, ri.active,
                          ru.base_status
                     FROM reservation_items ri
                     JOIN resource_units ru ON ru.unit_id = ri.unit_id
                    WHERE ri.plan_id = ? AND ri.unit_id = ? AND ri.active = 1""",
                (plan_id, old_unit_id),
            ).fetchone()
            if item is None:
                raise BusinessError(
                    "allocation_not_found",
                    "the old unit is not actively allocated to this plan",
                    status_code=404,
                )
            sku = item["sku"]
            if replacement_unit_id:
                candidate = self._get_unit(self.conn, replacement_unit_id)
                if candidate["sku"] != sku:
                    raise BusinessError(
                        "invalid_replacement",
                        "replacement unit must belong to the same sku",
                        status_code=409,
                    )
                if candidate["base_status"] != "available":
                    raise BusinessError(
                        "invalid_replacement",
                        "replacement unit is not available",
                        status_code=409,
                    )
                if self.conn.execute(
                    """SELECT 1
                         FROM reservation_items ri
                         JOIN plans p ON p.plan_id = ri.plan_id
                        WHERE ri.unit_id = ?
                          AND ri.active = 1
                          AND p.status <> 'released'
                          AND ri.start_at < ?
                          AND ? < ri.end_at
                        LIMIT 1""",
                    (replacement_unit_id, item["end_at"], item["start_at"]),
                ).fetchone():
                    raise BusinessError(
                        "invalid_replacement",
                        "replacement unit is already allocated in the window",
                        status_code=409,
                    )
                new_unit_id = replacement_unit_id
            else:
                candidates = self._candidate_units(self.conn, sku, item["start_at"], item["end_at"])
                candidates = [unit for unit in candidates if unit != old_unit_id]
                if not candidates:
                    raise BusinessError(
                        "resource_unavailable",
                        "no replacement unit is available for the window",
                        status_code=409,
                        details={"sku": sku},
                    )
                new_unit_id = candidates[0]

            now = utc_now()
            self.conn.execute(
                """UPDATE reservation_items
                      SET active = 0, replaced_at = ?, replacement_reason = ?
                    WHERE plan_id = ? AND unit_id = ? AND active = 1""",
                (now, reason, plan_id, old_unit_id),
            )
            self.conn.execute(
                """INSERT INTO reservation_items(plan_id, sku, unit_id, start_at, end_at, active)
                   VALUES (?, ?, ?, ?, ?, 1)""",
                (plan_id, sku, new_unit_id, item["start_at"], item["end_at"]),
            )
            self.conn.execute(
                """INSERT INTO replacements(plan_id, old_unit_id, replacement_unit_id, sku, reason,
                                            idempotency_key, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (plan_id, old_unit_id, new_unit_id, sku, reason, idempotency_key, now),
            )
            self._audit(
                self.conn,
                "resource_replaced",
                plan_id=plan_id,
                request_id=idempotency_key,
                detail={
                    "sku": sku,
                    "old_unit_id": old_unit_id,
                    "replacement_unit_id": new_unit_id,
                    "reason": reason,
                },
            )
            self._commit()
            return 200, {
                "plan_id": plan_id,
                "old_unit_id": old_unit_id,
                "replacement_unit_id": new_unit_id,
                "sku": sku,
                "reason": reason,
                "replayed": False,
            }
        except sqlite3.IntegrityError as exc:
            self._rollback()
            raise BusinessError(
                "reservation_conflict",
                "replacement conflicts with an existing allocation",
                status_code=409,
                details={"database_error": str(exc)},
            ) from exc
        except Exception:
            self._rollback()
            raise

    # ------------------------------------------------------------------
    # Release / close-out
    # ------------------------------------------------------------------
    def release_plan(self, plan_id: str) -> tuple[int, dict[str, Any]]:
        now = utc_now()
        self._begin()
        try:
            plan = self._get_plan(self.conn, plan_id)
            if plan["status"] == "released":
                body = {
                    "plan_id": plan_id,
                    "status": "released",
                    "released_at": plan["released_at"],
                    "released_units": [],
                    "already_released": True,
                }
                self._commit()
                return 200, body
            items = self._active_items(self.conn, plan_id)
            unit_ids = [item["unit_id"] for item in items]
            self.conn.execute(
                "UPDATE plans SET status = 'released', released_at = ? WHERE plan_id = ?",
                (now, plan_id),
            )
            # Allocation rows remain as history (active=0); the audit entry
            # below records exactly which units were freed by this close-out.
            self.conn.execute(
                "UPDATE reservation_items SET active = 0 WHERE plan_id = ? AND active = 1",
                (plan_id,),
            )
            self._audit(
                self.conn,
                "reservation_released",
                plan_id=plan_id,
                detail={"released_units": unit_ids, "released_at": now},
            )
            self._commit()
            return 200, {
                "plan_id": plan_id,
                "status": "released",
                "released_at": now,
                "released_units": unit_ids,
                "already_released": False,
            }
        except Exception:
            self._rollback()
            raise

    # ------------------------------------------------------------------
    # Queries and audit
    # ------------------------------------------------------------------
    def get_plan(self, plan_id: str) -> dict[str, Any]:
        plan = self._get_plan(self.conn, plan_id)
        return self._plan_payload(self.conn, plan)

    def list_plans(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT plan_id, segment, start_at, end_at, status, idempotency_key,
                      request_hash, created_at, released_at
                 FROM plans ORDER BY created_at, plan_id"""
        ).fetchall()
        return [self._plan_payload(self.conn, row) for row in rows]

    def list_audit(self, plan_id: Optional[str] = None) -> list[dict[str, Any]]:
        if plan_id:
            rows = self.conn.execute(
                """SELECT audit_id, event_id, event_type, plan_id, request_id, detail_json, created_at
                     FROM audit_log WHERE plan_id = ? ORDER BY audit_id""",
                (plan_id,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """SELECT audit_id, event_id, event_type, plan_id, request_id, detail_json, created_at
                     FROM audit_log ORDER BY audit_id"""
            ).fetchall()
        return [
            {
                "audit_id": row["audit_id"],
                "event_id": row["event_id"],
                "event_type": row["event_type"],
                "plan_id": row["plan_id"],
                "request_id": row["request_id"],
                "detail": json.loads(row["detail_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]
