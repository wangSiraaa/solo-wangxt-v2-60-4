"""既有区段互斥规则（本模块为既有基线，不随资源预约能力修改）。

互斥矩阵声明哪些区段不允许在时间重叠时并行演练：
- 对称矩阵；区段与自身恒互斥（同一区段不可并行）；
- 值 True 表示互斥，False 表示允许并行。

A/B/C 为室内受控区段（共用逃生通道、相邻高压配电房）；
D 与 Z1..Z8 为相互隔离的露天集结/作业区，可并行演练，
但它们共用的稀缺防护物资/设备仍受预约层约束（不超卖）。
"""
from __future__ import annotations

INDOOR_SECTIONS = ("SECTION_A", "SECTION_B", "SECTION_C")
OPEN_SECTION = "SECTION_D"
ZONE_SECTIONS = tuple(f"SECTION_Z{i}" for i in range(1, 9))

SECTIONS = INDOOR_SECTIONS + (OPEN_SECTION,) + ZONE_SECTIONS


def _build_matrix() -> dict[str, dict[str, bool]]:
    matrix: dict[str, dict[str, bool]] = {s: {} for s in SECTIONS}
    for s1 in SECTIONS:
        for s2 in SECTIONS:
            if s1 == s2:
                matrix[s1][s2] = True                 # 同区段不可并行
                continue
            indoor = {*INDOOR_SECTIONS}
            if s1 in indoor and s2 in indoor:
                matrix[s1][s2] = True                 # 室内区段两两互斥
            else:
                matrix[s1][s2] = False                # 其余（含露天区段之间、露天-室内）允许并行
    return matrix


MUTEX_MATRIX = _build_matrix()


def known_section(section: str) -> bool:
    return section in MUTEX_MATRIX


def sections_conflict(s1: str, s2: str) -> bool:
    """两个区段在矩阵中是否互斥。"""
    return bool(MUTEX_MATRIX[s1][s2])


def windows_overlap(start1: str, end1: str, start2: str, end2: str) -> bool:
    """半开区间重叠判定；首尾相接（[..,t) 与 [t,..)）不算重叠。"""
    return start1 < end2 and start2 < end1


def plans_conflict(plan1, plan2) -> bool:
    """两个演练计划是否冲突：窗口重叠 且 区段在互斥矩阵中互斥。

    互斥矩阵允许并行（False）的区段，即使窗口重叠也不冲突；
    资源层的稀缺性冲突不在本函数职责内（见 store.py/service.py）。
    """
    if not windows_overlap(plan1["window_start"], plan1["window_end"],
                           plan2["window_start"], plan2["window_end"]):
        return False
    return sections_conflict(plan1["section"], plan2["section"])
