"""Application-level error type shared by service and HTTP layers."""

from __future__ import annotations

from typing import Any


class BusinessError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 409,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        result = {"error": {"code": self.code, "message": self.message}}
        if self.details:
            result["error"]["details"] = self.details
        return result
