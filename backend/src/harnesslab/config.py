"""配置层级（文档 08 第 3 节）。

优先级：内置默认值 < 无密钥配置文件 configs/app.config.json < 环境变量。
启动时验证并输出脱敏摘要，密钥只来自环境变量或本机秘密管理。
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_FILE = REPO_ROOT / "configs" / "app.config.json"

SECRET_FIELDS = {"embedding_api_key", "chat_api_key"}


class _JsonConfigSource(PydanticBaseSettingsSource):
    """无密钥配置文件来源，位于默认值和环境变量之间。"""

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:  # pragma: no cover
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        if not CONFIG_FILE.exists():
            return {}
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {k: v for k, v in data.items() if k not in SECRET_FIELDS}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        env_prefix="",
    )

    # ---- 运行环境 ----
    app_env: Literal["development", "test", "production"] = "development"
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    dev_owner_id: str = "local-dev-user"
    cors_origins: str = "http://127.0.0.1:5173,http://localhost:5173"

    # ---- 存储 ----
    data_dir: Path = Path("data")
    artifact_dir: Path = Path("artifacts")
    business_db_url: str = ""
    checkpoint_db_url: str = ""
    vector_mode: Literal["local", "service"] = "local"
    # 缺省落在 DATA_DIR 下；显式配置时按 REPO_ROOT 解析
    qdrant_path: Path | None = None
    vector_collection: str = "harnesslab_chunks"

    # ---- Embedding（文档 04）----
    embedding_provider: Literal["lm_studio", "openai_compatible", "fake"] = "lm_studio"
    embedding_base_url: str = "http://localhost:1234/v1"
    embedding_model: str = "replace-with-real-embedding-model-id"
    embedding_api_key: str = ""
    embedding_dimension: str = "auto"
    embedding_batch_size: int = 8
    embedding_timeout_seconds: float = 60.0
    embedding_max_retries: int = 2
    embedding_query_prefix: str = ""
    embedding_document_prefix: str = ""
    embedding_normalize: bool = True

    # ---- Chat（文档 04 / 03 第 6 节）----
    chat_provider: Literal["openai_compatible", "stub"] = "openai_compatible"
    chat_base_url: str = "http://localhost:1234/v1"
    chat_model: str = "replace-with-real-chat-model-id"
    chat_api_key: str = ""
    chat_temperature: float = 0.0
    chat_context_window: int = 8192
    chat_max_output_tokens: int = 2048
    allow_cloud_fallback: bool = False
    model_max_concurrency: int = 1
    max_subagent_concurrency: int = 2
    worker_concurrency: int = 1

    # ---- 预算（文档 03 第 9 节）----
    budget_max_model_calls: int = 20
    budget_max_tool_calls: int = 30
    budget_active_timeout_seconds: int = 180
    budget_tool_timeout_seconds: int = 30
    budget_max_retries: int = 2
    budget_max_replans: int = 2
    budget_max_subtasks: int = 4
    budget_max_depth: int = 1
    budget_input_tokens: int = 60000
    budget_output_tokens: int = 12000
    context_compress_ratio: float = 0.8

    # ---- 检索（文档 04 第 7 节）----
    retrieval_mode: Literal["vector", "hybrid"] = "hybrid"
    retrieval_top_k: int = 8
    retrieval_candidates: int = 20
    rrf_constant: int = 60
    min_evidence_score: float = 0.15

    # ---- 导入（文档 04 第 5 节）----
    max_upload_bytes: int = 20 * 1024 * 1024
    max_pdf_pages: int = 200
    chunk_target_tokens: int = 600
    chunk_overlap_tokens: int = 80
    pipeline_version: str = "chunker-v1"

    # ---- 运行与策略 ----
    lease_seconds: int = 60
    # 单机开发默认让 API 进程内联工作进程，避免两个进程同时打开 Local 向量库目录
    run_worker_in_api: bool = True
    run_event_poll_seconds: float = 0.4
    run_event_retention_days: int = 30
    approval_ttl_hours: int = 24
    worker_poll_seconds: float = 0.5
    workspace_max_files: int = 50
    workspace_max_file_bytes: int = 5 * 1024 * 1024
    artifact_keep_versions: int = 5

    # ---- 观测与扩展 ----
    trace_export_enabled: bool = False
    sandbox_enabled: bool = False
    fault_injection: str = Field(default="", description="开发环境故障注入点；生产必须为空")
    log_level: str = "INFO"

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """优先级：环境变量 > .env > 无密钥配置文件 > 内置默认值。"""
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            _JsonConfigSource(settings_cls),
            file_secret_settings,
        )

    # ------------------------------------------------------------------
    @field_validator("data_dir", "artifact_dir", mode="before")
    @classmethod
    def _no_empty_path(cls, value: Any) -> Any:
        return value or "."

    @model_validator(mode="after")
    def _resolve_and_validate(self) -> Settings:
        self.data_dir = self._absolute(self.data_dir)
        self.artifact_dir = self._absolute(self.artifact_dir)
        self.qdrant_path = (
            self.data_dir / "qdrant" if self.qdrant_path is None else self._absolute(self.qdrant_path)
        )
        if not self.business_db_url:
            self.business_db_url = f"sqlite:///{(self.data_dir / 'business.db').as_posix()}"
        if not self.checkpoint_db_url:
            self.checkpoint_db_url = f"sqlite:///{(self.data_dir / 'checkpoints.db').as_posix()}"
        if self.app_env == "production" and self.fault_injection:
            raise ValueError("生产环境不允许启用故障注入")
        if self.embedding_dimension != "auto":
            try:
                if int(self.embedding_dimension) <= 0:
                    raise ValueError
            except ValueError as exc:  # pragma: no cover - 配置错误
                raise ValueError("EMBEDDING_DIMENSION 必须是 auto 或正整数") from exc
        return self

    @staticmethod
    def _absolute(path: Path) -> Path:
        path = Path(path)
        return path if path.is_absolute() else (REPO_ROOT / path).resolve()

    # ------------------------------------------------------------------
    @property
    def business_db_path(self) -> Path:
        return Path(self.business_db_url.removeprefix("sqlite:///"))

    @property
    def checkpoint_db_path(self) -> Path:
        return Path(self.checkpoint_db_url.removeprefix("sqlite:///"))

    @property
    def workspace_root(self) -> Path:
        return self.data_dir / "workspaces"

    @property
    def upload_root(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    def redacted(self) -> dict[str, Any]:
        """脱敏摘要：密钥只显示是否已配置。"""
        data = self.model_dump(mode="json")
        for field in SECRET_FIELDS:
            value = data.get(field) or ""
            data[field] = f"<已配置 {len(value)} 字符>" if value else ""
        return data

    def ensure_directories(self) -> None:
        for path in (
            self.data_dir,
            self.artifact_dir,
            self.workspace_root,
            self.upload_root,
            self.qdrant_path,
            self.data_dir / "staging",
        ):
            Path(path).mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings


def reset_settings_cache() -> None:
    """测试用：重新读取配置。"""
    get_settings.cache_clear()
