"""统一 API 错误与校验辅助。"""
from __future__ import annotations

import json


class ApiError(Exception):
    def __init__(self, code: str, message: str, http_status: int = 400, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.details = details or []

    def body(self) -> dict:
        return {"error": {"code": self.code, "message": self.message, "details": self.details}}


def require(body: dict, field: str, expected_type):
    if not isinstance(body, dict) or field not in body:
        raise ApiError("VALIDATION_ERROR", f"缺少必填字段: {field}", 400)
    value = body[field]
    if not isinstance(value, expected_type) or (expected_type is int and isinstance(value, bool)):
        type_name = getattr(expected_type, "__name__", str(expected_type))
        raise ApiError("VALIDATION_ERROR", f"字段 {field} 类型必须为 {type_name}", 400)
    return value


def dumps(data) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))
