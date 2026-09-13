"""健康检查与模型档案（文档 05 第 3 节）。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response

from ..harness.models import probe_chat_capabilities
from ..knowledge.embeddings import build_embedding_service
from ..storage.db import get_database
from .deps import RepoDep, SettingsDep

router = APIRouter(tags=["health"])


@router.get("/health/live")
def live() -> dict[str, Any]:
    return {"status": "alive"}


@router.get("/health/ready")
def ready(response: Response, repo: RepoDep, settings: SettingsDep) -> dict[str, Any]:
    """必需存储可用才算就绪；模型状态、检查点库等作为独立提示项列出。"""
    required: dict[str, Any] = {}
    advisory: dict[str, Any] = {}
    try:
        database = get_database()
        required["business_db"] = {
            "ok": True,
            "schema_version": database.schema_version,
            "path_kind": "sqlite",
        }
    except Exception as exc:
        required["business_db"] = {"ok": False, "error": type(exc).__name__}

    advisory["checkpoint_db"] = {
        "ok": settings.checkpoint_db_path.exists(),
        "advisory": True,
        "file": settings.checkpoint_db_path.name,
        "note": "工作进程首次启动时会创建并初始化检查点库",
    }
    advisory["model_status"] = {
        "advisory": True,
        "chat": {
            "provider": settings.chat_provider,
            "model": settings.chat_model,
            "note": "模型探测通过 POST /model-profiles/{id}/probe 主动执行",
        },
        "embedding": {
            "provider": settings.embedding_provider,
            "model": settings.embedding_model,
            "configured_dimension": settings.embedding_dimension,
            "note": "模型不可用不影响接口就绪，但检索与问答会返回 EMBEDDING_UNAVAILABLE",
        },
    }
    ready_ok = all(item["ok"] for item in required.values())
    if not ready_ok:
        response.status_code = 503
    return {
        "status": "ready" if ready_ok else "degraded",
        "checks": {**required, **advisory},
        "policy": "policy-v1",
    }


@router.get("/model-profiles")
def model_profiles(settings: SettingsDep) -> dict[str, Any]:
    return {
        "profiles": [
            {
                "id": "local-chat",
                "role": "chat",
                "provider": settings.chat_provider,
                "model": settings.chat_model,
                "base_url": settings.chat_base_url,
                "api_key_configured": bool(settings.chat_api_key),
                "context_window": settings.chat_context_window,
                "max_concurrency": settings.model_max_concurrency,
                "capabilities": {
                    "chat": settings.chat_provider != "stub",
                    "streaming": settings.chat_provider != "stub",
                    "tool_calling": settings.chat_provider != "stub",
                    "structured_output": settings.chat_provider != "stub",
                    "probed": False,
                },
                "note": "能力只有通过实测探测才会被标记为可用",
            },
            {
                "id": "local-embedding",
                "role": "embedding",
                "provider": settings.embedding_provider,
                "model": settings.embedding_model,
                "base_url": settings.embedding_base_url,
                "api_key_configured": bool(settings.embedding_api_key),
                "dimension": settings.embedding_dimension,
                "normalize": settings.embedding_normalize,
                "query_prefix": settings.embedding_query_prefix,
                "document_prefix": settings.embedding_document_prefix,
                "batch_size": settings.embedding_batch_size,
                "capabilities": {"probed": False},
            },
        ],
        "note": "接口只返回脱敏配置，密钥永不返回",
    }


@router.post("/model-profiles/{profile_id}/probe", status_code=202)
def probe_profile(profile_id: str, settings: SettingsDep) -> dict[str, Any]:
    """同步执行一次探测并返回结果；本机探测耗时短，无需占用后台队列。"""
    if profile_id == "local-chat":
        result = probe_chat_capabilities(settings)
        return {"profile_id": profile_id, "result": result}
    if profile_id == "local-embedding":
        service = build_embedding_service(settings)
        result = service.probe()
        return {
            "profile_id": profile_id,
            "result": {
                "model": result["model"],
                "dimension": result["dimension"],
                "normalize": result["normalize"],
                "batch_size": result["batch_size"],
                "note": "维度用于写入索引清单；更换模型必须重建索引",
            },
        }
    return {"profile_id": profile_id, "result": {"status": "unknown", "note": "未知档案"}}
