"""本地结构化日志；默认不上传文档与消息。"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

SENSITIVE_KEYS = {
    "authorization",
    "api_key",
    "apikey",
    "token",
    "password",
    "cookie",
    "embedding_api_key",
    "chat_api_key",
}


def redact(data: Any) -> Any:
    if isinstance(data, dict):
        return {
            key: ("<redacted>" if key.lower() in SENSITIVE_KEYS else redact(value))
            for key, value in data.items()
        }
    if isinstance(data, (list, tuple)):
        return [redact(item) for item in data]
    if isinstance(data, str) and len(data) > 2000:
        return data[:2000] + "...<truncated>"
    return data


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            payload.update(redact(extra))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


_configured = False


def configure_logging(level: str = "INFO") -> None:
    global _configured
    if _configured:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger("harnesslab")
    root.handlers = [handler]
    root.setLevel(level.upper())
    root.propagate = False
    _configured = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"harnesslab.{name}")


def log_event(logger: logging.Logger, message: str, **fields: Any) -> None:
    logger.info(message, extra={"extra_fields": fields})
