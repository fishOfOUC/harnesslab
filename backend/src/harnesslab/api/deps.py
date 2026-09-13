"""依赖装配与身份上下文（文档 10 第 1 节：服务端身份与项目作用域是权限依据）。"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, Header, Request

from ..config import Settings, get_settings
from ..errors import HarnessLabError, NotFoundError
from ..knowledge.service import KnowledgeService
from ..observability.events import EventSink
from ..storage.db import get_database
from ..storage.repositories import Repository


@dataclass
class Identity:
    """本机开发模式下的固定身份；公开网络部署前必须替换为正式认证。"""

    user_id: str
    is_admin: bool = True


@lru_cache(maxsize=1)
def get_repository() -> Repository:
    return Repository(get_database())


@lru_cache(maxsize=1)
def get_knowledge_service() -> KnowledgeService:
    return KnowledgeService(get_repository(), get_settings())


@lru_cache(maxsize=1)
def get_event_sink() -> EventSink:
    return EventSink(get_repository())


def get_identity(
    request: Request,
    x_dev_user: Annotated[str | None, Header(alias="X-Dev-User")] = None,
) -> Identity:
    settings: Settings = request.app.state.settings
    # 只绑定 loopback 时才允许开发身份；请求头仅用于本地多身份调试
    client_host = request.client.host if request.client else ""
    if client_host not in {"127.0.0.1", "::1", "localhost", "testclient"}:
        raise HarnessLabError("FORBIDDEN", "非本机访问需要正式认证后才能启用")
    return Identity(user_id=x_dev_user or settings.dev_owner_id)


def require_project(repo: Repository, project_id: str, identity: Identity) -> dict:
    project = repo.get_project(project_id)
    if project["owner_id"] != identity.user_id and not identity.is_admin:
        raise HarnessLabError("FORBIDDEN", "无权访问该项目")
    return project


RepoDep = Annotated[Repository, Depends(get_repository)]
KnowledgeDep = Annotated[KnowledgeService, Depends(get_knowledge_service)]
IdentityDep = Annotated[Identity, Depends(get_identity)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def ensure_run_scope(repo: Repository, run_id: str, identity: Identity) -> dict:
    run = repo.get_run(run_id)
    require_project(repo, run["project_id"], identity)
    return run


def require_thread(repo: Repository, thread_id: str, identity: Identity) -> dict:
    try:
        thread = repo.get_thread(thread_id)
    except NotFoundError:
        raise
    require_project(repo, thread["project_id"], identity)
    return thread
