"""统一错误契约（对应文档 05 第 7 节）。

错误信息不包含密钥、宿主绝对路径或完整堆栈。
"""

from __future__ import annotations

from typing import Any

# 错误码 -> HTTP 状态；已接受的异步任务失败改由 run/job 状态报告业务错误。
HTTP_STATUS: dict[str, int] = {
    "VALIDATION_ERROR": 422,
    "UNAUTHORIZED": 401,
    "FORBIDDEN": 403,
    "NOT_FOUND": 404,
    "RUN_CONFLICT": 409,
    "APPROVAL_CONFLICT": 409,
    "INDEX_VERSION_MISMATCH": 409,
    "MODEL_CAPABILITY_MISSING": 422,
    "RATE_LIMITED": 429,
    "EMBEDDING_UNAVAILABLE": 503,
    "MODEL_UNAVAILABLE": 503,
    "STORAGE_UNAVAILABLE": 503,
    "PATH_ESCAPE": 403,
    "TOOL_NOT_FOUND": 404,
    "TOOL_TIMEOUT": 504,
    "SANDBOX_UNAVAILABLE": 503,
}

# 业务错误（在 run/job 终态里出现）
BUSINESS_ERRORS: dict[str, int] = {
    "BUDGET_EXCEEDED": 0,
    "TOOL_TIMEOUT": 0,
    "OUTPUT_VALIDATION_FAILED": 0,
    "SIDE_EFFECT_UNCERTAIN": 0,
    "TOOL_FAILED": 0,
    "CANCELLED": 0,
    "INTERNAL_ERROR": 0,
}

RETRYABLE_DEFAULT = {
    "EMBEDDING_UNAVAILABLE": True,
    "MODEL_UNAVAILABLE": True,
    "STORAGE_UNAVAILABLE": True,
    "TOOL_TIMEOUT": True,
    "RATE_LIMITED": True,
    "SIDE_EFFECT_UNCERTAIN": False,
    "VALIDATION_ERROR": False,
    "FORBIDDEN": False,
    "UNAUTHORIZED": False,
    "RUN_CONFLICT": False,
    "APPROVAL_CONFLICT": False,
    "INDEX_VERSION_MISMATCH": False,
    "MODEL_CAPABILITY_MISSING": False,
    "BUDGET_EXCEEDED": False,
    "PATH_ESCAPE": False,
    "TOOL_NOT_FOUND": False,
}


class HarnessLabError(Exception):
    """项目统一异常。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = RETRYABLE_DEFAULT.get(code, False) if retryable is None else retryable
        self.details = details or {}

    @property
    def http_status(self) -> int:
        return HTTP_STATUS.get(self.code, 400)

    def to_payload(self, request_id: str | None = None) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "retryable": self.retryable,
                "request_id": request_id,
                "details": self.details,
            }
        }


class NotFoundError(HarnessLabError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("NOT_FOUND", message, details=details)


class ConflictError(HarnessLabError):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(code, message, details=details)


class ValidationFailure(HarnessLabError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("VALIDATION_ERROR", message, details=details)
