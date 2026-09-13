"""统一命令入口（文档 08 第 4 节）。

实现：doctor / db migrate / api serve / worker serve / demo seed /
index rebuild / eval run / data export / data restore。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import zipfile
from pathlib import Path
from typing import Any

from .config import Settings, get_settings
from .observability.logging import configure_logging
from .storage.db import Database, get_database, reset_database
from .storage.repositories import Repository

EXIT_OK = 0
EXIT_FAIL = 1


# --------------------------------------------------------------------- doctor
def cmd_doctor(args: argparse.Namespace) -> int:
    settings = get_settings()
    configure_logging(settings.log_level)
    checks: list[tuple[str, bool, str]] = []

    checks.append(
        (
            "配置加载",
            True,
            f"APP_ENV={settings.app_env}，数据目录={_display(settings.data_dir)}",
        )
    )
    checks.append(
        (
            "数据目录可写",
            _writable(settings.data_dir),
            _display(settings.data_dir),
        )
    )
    checks.append(
        (
            "产物目录可写",
            _writable(settings.artifact_dir),
            _display(settings.artifact_dir),
        )
    )
    try:
        database = get_database()
        checks.append(("业务库", True, f"schema_version={database.schema_version}"))
    except Exception as exc:
        checks.append(("业务库", False, f"{type(exc).__name__}: {exc}"))
    checks.append(
        (
            "检查点库目录",
            settings.checkpoint_db_path.parent.exists(),
            _display(settings.checkpoint_db_path.parent),
        )
    )

    if args.probe:
        from .harness.models import probe_chat_capabilities
        from .knowledge.embeddings import build_embedding_service

        try:
            probe = build_embedding_service(settings).probe()
            checks.append(
                ("Embedding 探测", True, f"model={probe['model']}，dimension={probe['dimension']}")
            )
        except Exception as exc:
            checks.append(("Embedding 探测", False, f"{type(exc).__name__}: {__import__('builtins').str(exc)}"))
        chat = probe_chat_capabilities(settings)
        checks.append(
            (
                "Chat 探测",
                chat["status"] == "ok",
                f"status={chat['status']}，capabilities={chat['capabilities']}",
            )
        )
    else:
        checks.append(("模型探测", True, "已跳过（加 --probe 执行真实探测）"))

    print("HarnessLab 环境检查")
    print("-" * 72)
    for name, ok, detail in checks:
        mark = "OK  " if ok else "FAIL"
        print(f"[{mark}] {name}: {detail}")
    failed = [name for name, ok, _ in checks if not ok]
    if failed:
        print(f"\n存在 {len(failed)} 项异常：{', '.join(failed)}")
        return EXIT_FAIL
    print("\n配置脱敏摘要：")
    print(json.dumps(settings.redacted(), ensure_ascii=False, indent=2))
    return EXIT_OK


# --------------------------------------------------------------------- db
def cmd_db_migrate(args: argparse.Namespace) -> int:
    settings = get_settings()
    database = get_database()
    from .agents.factory import build_checkpointer

    build_checkpointer(settings)
    print(f"业务库迁移完成：{_display(settings.business_db_path)}（schema v{database.schema_version}）")
    print(f"检查点库初始化完成：{_display(settings.checkpoint_db_path)}")
    return EXIT_OK


# --------------------------------------------------------------------- serve
def cmd_api_serve(args: argparse.Namespace) -> int:
    import uvicorn

    settings = get_settings()
    configure_logging(settings.log_level)
    if not args.probe and not settings.chat_api_key and settings.chat_provider != "stub":
        print("[提示] 未配置 CHAT_API_KEY；LM Studio 无认证时留空即可。")
    from .api.app import create_app

    app = create_app(settings, with_worker=None if args.worker is None else args.worker)
    uvicorn.run(app, host=args.host or settings.api_host, port=args.port or settings.api_port, log_level="info")
    return EXIT_OK


def cmd_worker_serve(args: argparse.Namespace) -> int:
    from .runtime.worker import Worker

    settings = get_settings()
    configure_logging(settings.log_level)
    if settings.vector_mode == "local":
        print(
            "[提示] 本地向量库只允许单进程访问。若 API 已在运行，请改用 "
            "`harnesslab api serve`（默认内联工作进程），或把 VECTOR_MODE 切到 service。"
        )
    worker = Worker(Repository(get_database()), settings)

    async def _run() -> None:
        try:
            await worker.serve()
        except KeyboardInterrupt:  # pragma: no cover - 交互退出
            worker.stop()

    asyncio.run(_run())
    return EXIT_OK


# --------------------------------------------------------------------- demo
def cmd_demo_seed(args: argparse.Namespace) -> int:
    from .demo import enqueue_demo_seed

    settings = get_settings()
    repo = Repository(get_database())
    project = _resolve_project(repo, args.project)
    result = enqueue_demo_seed(repo, settings, project["id"])
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("\n排队完成。请运行 `harnesslab worker serve`（或 api serve 内联）执行解析与向量化。")
    return EXIT_OK


# --------------------------------------------------------------------- index
def cmd_index_rebuild(args: argparse.Namespace) -> int:
    from .knowledge.service import KnowledgeService

    settings = get_settings()
    repo = Repository(get_database())
    project = _resolve_project(repo, args.project)
    service = KnowledgeService(repo, settings)
    result = service.rebuild_index(project["id"])
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return EXIT_OK


# --------------------------------------------------------------------- eval
def cmd_eval_run(args: argparse.Namespace) -> int:
    from .evals.runner import run_eval

    settings = get_settings()
    repo = Repository(get_database())
    project = _resolve_project(repo, args.project)
    dataset = Path(args.dataset) if args.dataset else None
    report = run_eval(repo, settings, project["id"], dataset=dataset, mode=args.mode)
    output = settings.artifact_dir / "evals" / f"eval-{report['eval_id']}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["metrics"], ensure_ascii=False, indent=2))
    print(f"\n完整报告：{_display(output)}")
    return EXIT_OK


# --------------------------------------------------------------------- 备份
def cmd_data_export(args: argparse.Namespace) -> int:
    settings = get_settings()
    target = Path(args.output) if args.output else settings.data_dir / "exports" / "harnesslab-backup.zip"
    target.parent.mkdir(parents=True, exist_ok=True)
    included: list[str] = []
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in (
            settings.business_db_path,
            settings.checkpoint_db_path,
            settings.data_dir / "uploads",
            settings.artifact_dir,
            settings.data_dir / "workspaces",
        ):
            if path.is_file():
                archive.write(path, path.name)
                included.append(path.name)
            elif path.is_dir():
                for file in path.rglob("*"):
                    if file.is_file():
                        archive.write(file, str(Path(path.name) / file.relative_to(path)))
                        included.append(str(file.relative_to(path.parent)))
    print(json.dumps({"archive": _display(target), "entries": len(included)}, ensure_ascii=False, indent=2))
    print("注意：备份包含业务库、检查点、原文与产物；恢复后需校验哈希与未完成操作记录。")
    return EXIT_OK


def cmd_data_restore(args: argparse.Namespace) -> int:
    settings = get_settings()
    source = Path(args.archive)
    if not source.exists():
        print(f"备份文件不存在：{_display(source)}")
        return EXIT_FAIL
    reset_database()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.artifact_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(source) as archive:
        archive.extractall(settings.data_dir)
    reset_database()
    database = Database(settings.business_db_path)
    print(
        json.dumps(
            {
                "restored_from": _display(source),
                "schema_version": database.schema_version,
                "note": "恢复完成后请运行 harnesslab doctor 校验",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return EXIT_OK


# --------------------------------------------------------------------- helpers
def _writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".harnesslab-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def _display(path: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def _resolve_project(repo: Repository, identifier: str | None) -> dict[str, Any]:
    projects = repo.list_projects()
    if identifier:
        for project in projects:
            if project["id"] == identifier or project["name"] == identifier:
                return project
        raise SystemExit(f"未找到项目：{identifier}")
    if projects:
        return projects[0]
    return repo.create_project("HarnessLab 演示项目", get_settings().dev_owner_id)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="harnesslab", description="HarnessLab 统一命令入口")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="检查配置、存储目录与模型连通性")
    doctor.add_argument("--probe", action="store_true", help="执行真实 Embedding / Chat 探测")
    doctor.set_defaults(func=cmd_doctor)

    db = sub.add_parser("db", help="数据库迁移")
    db_sub = db.add_subparsers(dest="db_command", required=True)
    migrate = db_sub.add_parser("migrate", help="执行业务迁移并初始化检查点库")
    migrate.set_defaults(func=cmd_db_migrate)

    api = sub.add_parser("api", help="接口服务")
    api_sub = api.add_subparsers(dest="api_command", required=True)
    serve = api_sub.add_parser("serve", help="启动 API 与 SSE")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--worker", dest="worker", action="store_true", help="在 API 进程内联工作进程")
    serve.add_argument("--no-worker", dest="worker", action="store_false", help="只启动 API")
    serve.set_defaults(worker=None, probe=False, func=cmd_api_serve)

    worker = sub.add_parser("worker", help="工作进程")
    worker_sub = worker.add_subparsers(dest="worker_command", required=True)
    worker_serve = worker_sub.add_parser("serve", help="领取任务、维持租约与故障恢复")
    worker_serve.set_defaults(func=cmd_worker_serve)

    demo = sub.add_parser("demo", help="演示数据")
    demo_sub = demo.add_subparsers(dest="demo_command", required=True)
    seed = demo_sub.add_parser("seed", help="幂等创建合成演示资料")
    seed.add_argument("--project", default=None, help="项目 ID 或名称，缺省用第一个项目")
    seed.set_defaults(func=cmd_demo_seed)

    index = sub.add_parser("index", help="索引维护")
    index_sub = index.add_subparsers(dest="index_command", required=True)
    rebuild = index_sub.add_parser("rebuild", help="暂存重建并切换活动索引")
    rebuild.add_argument("--project", default=None)
    rebuild.set_defaults(func=cmd_index_rebuild)

    evals = sub.add_parser("eval", help="评测")
    eval_sub = evals.add_subparsers(dest="eval_command", required=True)
    eval_run = eval_sub.add_parser("run", help="按数据集运行检索评测")
    eval_run.add_argument("--project", default=None)
    eval_run.add_argument("--dataset", default=None)
    eval_run.add_argument("--mode", default=None, choices=["vector", "hybrid", None])
    eval_run.set_defaults(func=cmd_eval_run)

    data = sub.add_parser("data", help="数据备份")
    data_sub = data.add_subparsers(dest="data_command", required=True)
    export = data_sub.add_parser("export", help="导出业务库、检查点、原文与产物")
    export.add_argument("--output", default=None)
    export.set_defaults(func=cmd_data_export)
    restore = data_sub.add_parser("restore", help="从备份恢复")
    restore.add_argument("archive")
    restore.set_defaults(func=cmd_data_restore)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    settings: Settings = get_settings()
    settings.ensure_directories()
    try:
        return int(args.func(args))
    except KeyboardInterrupt:  # pragma: no cover
        print("\n已中断。")
        return EXIT_OK
    except SystemExit as exc:  # pragma: no cover
        print(str(exc))
        return EXIT_FAIL
    except Exception as exc:
        print(f"[错误] {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_FAIL


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


def _copy_tree(source: Path, target: Path) -> None:  # pragma: no cover - 预留
    shutil.copytree(source, target, dirs_exist_ok=True)
