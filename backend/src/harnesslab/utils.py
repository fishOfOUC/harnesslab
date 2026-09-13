"""通用工具：时间、ID、哈希与估算器。"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import UTC, datetime
from typing import Any


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime | None = None) -> str:
    return (dt or utcnow()).isoformat().replace("+00:00", "Z")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def new_id() -> str:
    return str(uuid.uuid4())


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def json_loads(value: Any, default: Any = None) -> Any:
    """容错解析 JSON 字段：已解析的值原样返回，避免重复解析导致类型错误。"""
    if value is None or value == "":
        return default if default is not None else {}
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return default if default is not None else {}
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default if default is not None else {}


def sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def hash_payload(payload: Any) -> str:
    return sha256_text(json_dumps(payload))


_CJK = re.compile(r"[\u3000-\u9fff\uf900-\ufaff\uff00-\uffef]")
_LATIN_WORD = re.compile(r"[A-Za-z0-9_]+")


def estimate_tokens(text: str) -> int:
    """保守 token 估算（文档 04 第 5 节：无法获得准确 tokenizer 时标注估算）。

    中文按约 1 字 ≈ 0.7 token，英文按约 1 词 ≈ 1.3 token，再叠加标点余量。
    """
    if not text:
        return 0
    cjk = len(_CJK.findall(text))
    words = len(_LATIN_WORD.findall(text))
    others = max(len(text) - cjk - sum(len(w) for w in _LATIN_WORD.findall(text)), 0)
    estimate = cjk * 0.7 + words * 1.3 + others * 0.5
    return max(int(estimate) + 1, 1)


def truncate(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit], True
