"""FastAPI 应用装配（文档 05 第 3、7 节）。"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..config import REPO_ROOT, Settings, get_settings
from ..errors import HarnessLabError
from ..observability.logging import configure_logging, get_logger
from ..runtime.worker import Worker
from ..storage.db import get_database
from ..storage.repositories import Repository
from . import routes_health, routes_projects, routes_runs

logger = get_logger("api")


def create_app(settings: Settings | None = None, *, with_worker: bool | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    run_worker = settings.run_worker_in_api if with_worker is None else with_worker

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        worker: Worker | None = None
        task: asyncio.Task | None = None
        if run_worker:
            repo = Repository(get_database())
            worker = Worker(repo, settings)
            task = asyncio.create_task(worker.serve(), name="harnesslab-worker")
            logger.info("API 进程内联工作进程（共用同一向量库实例）")
        try:
            yield
        finally:
            if worker is not None:
                worker.stop()
            if task is not None:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(task, timeout=10)

    app = FastAPI(
        title="HarnessLab",
        version=__version__,
        description="面向技术展示的 LangChain / LangGraph Agent 工作台（单机单用户）",
        lifespan=lifespan,
    )
    app.state.settings = settings

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Idempotency-Key", "Last-Event-ID", "X-Dev-User"],
    )

    @app.exception_handler(HarnessLabError)
    async def handle_domain_error(request: Request, exc: HarnessLabError) -> JSONResponse:
        request_id = request.headers.get("X-Request-Id")
        return JSONResponse(status_code=exc.http_status, content=exc.to_payload(request_id))

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "请求参数校验失败",
                    "retryable": False,
                    "request_id": request.headers.get("X-Request-Id"),
                    "details": {"errors": exc.errors()[:10]},
                }
            },
        )

    api_prefix = "/api/v1"
    app.include_router(routes_health.router, prefix=api_prefix)
    app.include_router(routes_projects.router, prefix=api_prefix)
    app.include_router(routes_runs.router, prefix=api_prefix)

    @app.get("/api/v1/policy")
    def policy_snapshot() -> dict[str, Any]:
        from ..harness.policy import PolicyEngine

        return {"policies": PolicyEngine(sandbox_enabled=settings.sandbox_enabled).describe()}

    dist = REPO_ROOT / "frontend" / "dist"
    if dist.exists():
        app.mount("/", StaticFiles(directory=str(dist), html=True), name="frontend")
    else:

        @app.get("/")
        def index() -> dict[str, Any]:
            return {
                "name": "HarnessLab",
                "version": __version__,
                "note": "前端尚未构建；开发时请运行 frontend 的 dev server，或执行 npm run build",
                "docs": "/docs",
            }

    return app


app = create_app()
