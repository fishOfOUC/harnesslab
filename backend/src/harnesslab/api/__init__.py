"""鉴权、校验、任务提交、SSE 与下载；不直接执行模型和工具循环。"""

from .app import create_app

__all__ = ["create_app"]
