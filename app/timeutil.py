"""时间工具：统一使用 UTC、ISO-8601、半开区间 [start, end)。"""
from __future__ import annotations

from datetime import datetime, timezone


def now() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return format_dt(now())


def format_dt(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(value: str, field: str = "time") -> datetime:
    """解析 ISO-8601 时间，归一化为 UTC；非法输入抛出 ValueError。"""
    if not isinstance(value, str):
        raise ValueError(f"{field} 必须是 ISO-8601 字符串")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field} 不是合法的 ISO-8601 时间: {value}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso(value: str, field: str = "time") -> str:
    return format_dt(parse_iso(value, field))
