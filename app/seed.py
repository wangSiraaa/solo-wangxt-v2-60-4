"""初始种子数据：防护物资 + 演练设备（本地模拟，不依赖外部系统）。"""
from __future__ import annotations

from .timeutil import now_iso

# (编码, 名称, 类别, 数量)
SEED_RESOURCES = [
    ("MASK_N95",       "N95 防护口罩",   "MATERIAL",  10),
    ("PROTECTIVE_SUIT", "医用防护服",     "MATERIAL",  6),
    ("OXYGEN_KIT",     "便携式氧气瓶",   "EQUIPMENT", 3),
    ("GENSET_5KW",     "5kW 应急发电机", "EQUIPMENT", 2),
    ("RADIO_SET",      "防爆对讲机",     "EQUIPMENT", 4),
]


def seed_if_empty(conn) -> None:
    row = conn.execute("SELECT COUNT(*) AS n FROM resources").fetchone()
    if row["n"]:
        return
    ts = now_iso()
    for code, name, kind, total in SEED_RESOURCES:
        conn.execute(
            "INSERT INTO resources(code, name, kind, online, created_at, updated_at) "
            "VALUES (?, ?, ?, 1, ?, ?)",
            (code, name, kind, ts, ts),
        )
        for seq in range(1, total + 1):
            conn.execute(
                "INSERT INTO resource_units(resource_code, seq, status, created_at) "
                "VALUES (?, ?, 'AVAILABLE', ?)",
                (code, seq, ts),
            )
