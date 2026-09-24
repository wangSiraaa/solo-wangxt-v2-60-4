"""SQLite persistence and hard database constraints.

The service intentionally uses only the Python standard library.  SQLite runs in
WAL mode and all mutating business operations use ``BEGIN IMMEDIATE`` so that
concurrent HTTP workers serialize through the database writer instead of
overselling a scarce unit.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Union

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS metadata_kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS resource_skus (
    sku TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS resource_units (
    sku TEXT NOT NULL REFERENCES resource_skus(sku),
    unit_id TEXT PRIMARY KEY,
    base_status TEXT NOT NULL CHECK (base_status IN ('available','maintenance','retired')),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plans (
    plan_id TEXT PRIMARY KEY,
    segment TEXT NOT NULL,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('reserved','in_progress','released')),
    idempotency_key TEXT NOT NULL UNIQUE,
    request_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    released_at TEXT
);

CREATE TABLE IF NOT EXISTS plan_requirements (
    plan_id TEXT NOT NULL REFERENCES plans(plan_id),
    sku TEXT NOT NULL REFERENCES resource_skus(sku),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    PRIMARY KEY (plan_id, sku)
);

CREATE TABLE IF NOT EXISTS reservation_items (
    plan_id TEXT NOT NULL REFERENCES plans(plan_id),
    sku TEXT NOT NULL,
    unit_id TEXT NOT NULL REFERENCES resource_units(unit_id),
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    replaced_at TEXT,
    replacement_reason TEXT,
    PRIMARY KEY (plan_id, unit_id)
);

CREATE INDEX IF NOT EXISTS idx_items_lookup
    ON reservation_items(sku, unit_id, active, start_at, end_at);

CREATE INDEX IF NOT EXISTS idx_items_plan ON reservation_items(plan_id, active);

-- A unit cannot serve two non-released allocations whose half-open windows
-- overlap.  Adjacent windows ([a,b), [b,c)) are explicitly allowed.
CREATE TRIGGER IF NOT EXISTS trg_item_no_overlap_insert
BEFORE INSERT ON reservation_items
WHEN NEW.active = 1
BEGIN
    SELECT CASE
        WHEN EXISTS (
            SELECT 1
              FROM reservation_items old
             WHERE old.active = 1
               AND old.unit_id = NEW.unit_id
               AND old.start_at < NEW.end_at
               AND NEW.start_at < old.end_at
        )
        THEN RAISE(ABORT, 'overlapping reservation for resource unit')
    END;
END;

CREATE TRIGGER IF NOT EXISTS trg_item_no_overlap_update
BEFORE UPDATE OF unit_id, start_at, end_at, active ON reservation_items
WHEN NEW.active = 1
BEGIN
    SELECT CASE
        WHEN EXISTS (
            SELECT 1
              FROM reservation_items old
             WHERE old.active = 1
               AND old.unit_id = NEW.unit_id
               AND old.start_at < NEW.end_at
               AND NEW.start_at < old.end_at
               AND old.rowid <> NEW.rowid
        )
        THEN RAISE(ABORT, 'overlapping reservation for resource unit')
    END;
END;

CREATE TABLE IF NOT EXISTS segment_rules (
    segment_a TEXT NOT NULL,
    segment_b TEXT NOT NULL,
    can_parallel INTEGER NOT NULL CHECK (can_parallel IN (0,1)),
    CHECK (segment_a <= segment_b),
    PRIMARY KEY (segment_a, segment_b)
);

CREATE TABLE IF NOT EXISTS replacements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL REFERENCES plans(plan_id),
    old_unit_id TEXT NOT NULL,
    replacement_unit_id TEXT NOT NULL,
    sku TEXT NOT NULL,
    reason TEXT NOT NULL,
    idempotency_key TEXT UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    plan_id TEXT,
    request_id TEXT,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_plan_time ON audit_log(plan_id, created_at);
"""

DEFAULT_SEGMENTS = ["SECTION_A", "SECTION_B", "SECTION_C", "SECTION_D"]


def canonical_pair(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a <= b else (b, a)


def connect(database: Union[str, Path], timeout: float = 30.0) -> sqlite3.Connection:
    db_path = str(database)
    is_memory = db_path == ":memory:" or db_path.startswith("file:")
    if not is_memory:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        db_path,
        timeout=timeout,
        isolation_level=None,
        check_same_thread=False,
        uri=db_path.startswith("file:"),
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {int(timeout * 1000)}")
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL is not available for in-memory databases; persistent files use it.
    if not is_memory:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
    return conn


def initialize(conn: sqlite3.Connection) -> None:
    """Create schema and seed the local segment mutex matrix.

    Every known pair is explicit.  A deployment can alter a pair through a data
    migration, but this feature never mutates that matrix at runtime.  Unknown
    pairs are allowed by default because no local matrix entry forbids them.
    """

    conn.executescript(SCHEMA)
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT value FROM metadata_kv WHERE key='schema_version'"
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO metadata_kv(key, value) VALUES ('schema_version', '1')",
            )
        for i, a in enumerate(DEFAULT_SEGMENTS):
            for b in DEFAULT_SEGMENTS[i:]:
                x, y = canonical_pair(a, b)
                # The matrix itself is authoritative: a section excludes itself
                # during an overlap; distinct seeded sections may parallel.
                self_parallel = 0 if a == b else 1
                conn.execute(
                    """INSERT OR IGNORE INTO segment_rules(segment_a, segment_b, can_parallel)
                       VALUES (?, ?, ?)""",
                    (x, y, self_parallel),
                )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
