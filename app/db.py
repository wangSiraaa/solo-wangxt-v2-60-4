"""SQLite 持久化层：表结构、硬约束触发器与连接管理。

约束分级：
1. 应用事务（BEGIN IMMEDIATE，全局写串行）负责业务级“不超卖”；
2. 数据库触发器是最后防线：同一资源实例在重叠窗口上绝不允许出现两条
   有效承诺（held/blocked），维修失效实例不允许新增占用；
3. 审计日志表通过触发器保证只追加（append-only）。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- 资源目录：防护物资 / 演练设备
CREATE TABLE IF NOT EXISTS resources (
    code          TEXT PRIMARY KEY,                 -- 资源编码
    name          TEXT NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('MATERIAL','EQUIPMENT')),
    online        INTEGER NOT NULL DEFAULT 1,       -- 资源是否上线可用（下线=维修/失效）
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    version       INTEGER NOT NULL DEFAULT 1        -- 乐观版本号
);

-- 资源实例（每件可单独占用、维修、替换的实物）
CREATE TABLE IF NOT EXISTS resource_units (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_code TEXT NOT NULL REFERENCES resources(code),
    seq           INTEGER NOT NULL,                 -- 资源内序号
    status        TEXT NOT NULL DEFAULT 'AVAILABLE'
                  CHECK (status IN ('AVAILABLE','MAINTENANCE')),  -- 生命周期状态；OCCUPIED 按活动承诺实时推导
    created_at    TEXT NOT NULL,
    UNIQUE(resource_code, seq)
);

-- 演练计划
CREATE TABLE IF NOT EXISTS plans (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    section       TEXT NOT NULL,                    -- 受控区段（既有区段互斥维度）
    window_start  TEXT NOT NULL,
    window_end    TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'SCHEDULED'
                  CHECK (status IN ('SCHEDULED','IN_PROGRESS','COMPLETED','CANCELLED')),
    created_at    TEXT NOT NULL,
    CHECK (window_start < window_end)
);

-- 预约单（一个计划至多一条；幂等键唯一）
CREATE TABLE IF NOT EXISTS reservation_requests (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id         INTEGER NOT NULL UNIQUE REFERENCES plans(id),
    status          TEXT NOT NULL DEFAULT 'CONFIRMED'
                    CHECK (status IN ('CONFIRMED','RELEASED')),
    created_at      TEXT NOT NULL,
    released_at     TEXT
);

-- 预约明细行（每个资源一行；数量、占用窗口）
CREATE TABLE IF NOT EXISTS reservation_lines (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id        INTEGER NOT NULL REFERENCES reservation_requests(id),
    resource_code     TEXT NOT NULL REFERENCES resources(code),
    quantity          INTEGER NOT NULL CHECK (quantity > 0),
    window_start      TEXT NOT NULL,
    window_end        TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'HELD'
                      CHECK (status IN ('HELD','REPLACED','RELEASED')),
    replaces_line_id  INTEGER REFERENCES reservation_lines(id),
    created_at        TEXT NOT NULL,
    CHECK (window_start < window_end),
    UNIQUE(request_id, resource_code)               -- 同一计划同一资源仅一行
);

-- 资源实例占用承诺（明细行 ↔ 具体实例）
CREATE TABLE IF NOT EXISTS allocations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    line_id       INTEGER NOT NULL REFERENCES reservation_lines(id),
    unit_id       INTEGER NOT NULL REFERENCES resource_units(id),
    resource_code TEXT NOT NULL REFERENCES resources(code),
    window_start  TEXT NOT NULL,
    window_end    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_alloc_unit_window
    ON allocations(unit_id, window_start, window_end);
CREATE INDEX IF NOT EXISTS idx_alloc_line ON allocations(line_id);

-- 审计日志（append-only，由触发器强制）
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    action      TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id   TEXT NOT NULL,
    plan_id     INTEGER,
    status      TEXT NOT NULL DEFAULT 'SUCCESS',    -- SUCCESS / DENIED
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at);
CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_log(entity_type, entity_id);

-- 幂等键
CREATE TABLE IF NOT EXISTS idempotency_keys (
    idempotency_key TEXT PRIMARY KEY,
    method          TEXT NOT NULL,
    path            TEXT NOT NULL,
    request_hash    TEXT NOT NULL,
    response_code   INTEGER NOT NULL,
    response_body   TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

-- 触发器 1：硬防超卖/双订。
-- 同一实例上，任何与新承诺窗口重叠且仍有效（明细行未被替换/未释放）的
-- 承诺都必须被数据库拒绝。半开区间：首尾相接（end = 对方 start）允许复用。
CREATE TRIGGER IF NOT EXISTS trg_alloc_no_overlap
BEFORE INSERT ON allocations
WHEN EXISTS (
    SELECT 1
    FROM allocations a
    JOIN reservation_lines l ON l.id = a.line_id
    WHERE a.unit_id = NEW.unit_id
      AND l.status IN ('HELD','BLOCKED')
      AND NEW.window_start < a.window_end
      AND a.window_start < NEW.window_end
)
BEGIN
    SELECT RAISE(ABORT, 'ALLOC_OVERLAP: 资源实例在重叠窗口已被占用');
END;

-- 触发器 2：维修/失效实例不允许新增占用承诺。
CREATE TRIGGER IF NOT EXISTS trg_alloc_unit_ready
BEFORE INSERT ON allocations
WHEN EXISTS (
    SELECT 1 FROM resource_units u
    WHERE u.id = NEW.unit_id AND u.status <> 'AVAILABLE'
)
BEGIN
    SELECT RAISE(ABORT, 'UNIT_NOT_AVAILABLE: 资源实例处于维修/失效状态');
END;

-- 触发器 3：资源整体下线（维修/失效）时不允许新增占用承诺。
CREATE TRIGGER IF NOT EXISTS trg_alloc_resource_online
BEFORE INSERT ON allocations
WHEN EXISTS (
    SELECT 1 FROM resources r
    WHERE r.code = NEW.resource_code AND r.online = 0
)
BEGIN
    SELECT RAISE(ABORT, 'RESOURCE_OFFLINE: 资源已维修下线');
END;

-- 触发器 4-6：审计日志只追加，禁止 UPDATE/DELETE。
CREATE TRIGGER IF NOT EXISTS trg_audit_no_update
BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'AUDIT_IMMUTABLE: 审计日志不可修改');
END;
CREATE TRIGGER IF NOT EXISTS trg_audit_no_delete
BEFORE DELETE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'AUDIT_IMMUTABLE: 审计日志不可删除');
END;
"""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(path: str, *, seed: bool = True) -> sqlite3.Connection:
    conn = connect(path)
    conn.executescript(SCHEMA)
    if seed:
        from .seed import seed_if_empty
        seed_if_empty(conn)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    """写事务：BEGIN IMMEDIATE 立即获取 RESERVED 锁，使所有写者串行排队，
    配合触发器构成并发预约下的不超卖保证。

    允许业务代码在块内自行 COMMIT/ROLLBACK（例如“回滚占用但保留拒绝审计”
    的场景）；退出时仅当事务仍存在才再做收尾。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        if conn.in_transaction:
            conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
