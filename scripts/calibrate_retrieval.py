"""检索参数校准（文档 04 第 9 节、07 第 8 节）。

用途：用固定数据集对「query 前缀 / 检索模式 / 证据阈值」做一次真实模型对照，
输出可复现的对照表，并把实测结论写回 .env。禁止用测试集反复挑最优而不披露。

用法（在 backend 目录执行）：
    uv run python ../scripts/calibrate_retrieval.py
    uv run python ../scripts/calibrate_retrieval.py --modes hybrid vector --skip-import
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="HarnessLab 检索参数校准")
    parser.add_argument("--project", default=None, help="项目 ID 或名称，缺省用第一个项目")
    parser.add_argument("--modes", nargs="+", default=["hybrid", "vector"], help="要对比的检索模式")
    parser.add_argument("--skip-import", action="store_true", help="跳过演示资料导入（已有索引时使用）")
    parser.add_argument("--dataset", default=None, help="评测数据集路径")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    from harnesslab.config import get_settings
    from harnesslab.demo import enqueue_demo_seed
    from harnesslab.evals.runner import run_eval
    from harnesslab.knowledge.service import KnowledgeService
    from harnesslab.observability.logging import configure_logging
    from harnesslab.runtime.worker import Worker
    from harnesslab.storage.db import get_database
    from harnesslab.storage.repositories import Repository

    settings = get_settings()
    configure_logging("WARNING")
    repo = Repository(get_database())
    project = _resolve_project(repo, args.project)
    settings.ensure_directories()

    if not args.skip_import:
        seed = enqueue_demo_seed(repo, settings, project["id"])
        print(f"排队导入：{len(seed['queued'])} 份（已存在 {len(seed['already_indexed'])} 份）")
        _drain_jobs(repo, settings)
        print(f"索引完成：chunk {repo.count_chunks(project['id'])} 条")

    variants = [
        {"name": "无前缀（baseline）", "query_prefix": "", "document_prefix": ""},
        {
            "name": "Qwen3 官方指令前缀",
            "query_prefix": "Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery: ",
            "document_prefix": "",
        },
        {
            "name": "中文指令前缀",
            "query_prefix": "任务：根据中文检索问题，找出能够回答该问题的资料片段。\n查询：",
            "document_prefix": "",
        },
    ]

    dataset = Path(args.dataset) if args.dataset else None
    runs: list[dict[str, Any]] = []

    for variant in variants:
        settings.embedding_query_prefix = variant["query_prefix"]
        settings.embedding_document_prefix = variant["document_prefix"]
        knowledge = KnowledgeService(repo, settings)
        rebuild = knowledge.rebuild_index(project["id"])
        print(f"\n=== 变体：{variant['name']} ===")
        print(f"新索引 {rebuild['index_version'][:8]}，共 {rebuild['chunks']} 个 chunk，"
              f"指纹已更新（前缀参与指纹，故必须重建）")
        for mode in args.modes:
            report = run_eval(repo, settings, project["id"], dataset=dataset, mode=mode)
            metrics = report["metrics"]
            runs.append(
                {
                    "variant": variant["name"],
                    "query_prefix": variant["query_prefix"],
                    "mode": mode,
                    "index_version": report["index_version"],
                    "recall": metrics[f"recall_at_{report['top_k']}"],
                    "mrr": metrics[f"mrr_at_{report['top_k']}"],
                    "no_answer": metrics["no_answer_handling"],
                    "cases": report["case_results"],
                }
            )
            print(
                f"  mode={mode:6s} recall@{report['top_k']}={metrics[f'recall_at_{report["top_k"]}']} "
                f"mrr={metrics[f'mrr_at_{report["top_k"]}']} no_answer={metrics['no_answer_handling']}"
            )

    _print_summary(runs, settings.retrieval_top_k)
    _print_threshold_suggestion(runs)
    _write_report(runs, settings, project["id"])
    return 0


def _drain_jobs(repo: Any, settings: Any, limit: int = 20) -> None:
    from harnesslab.runtime.worker import Worker

    worker = Worker(repo, settings)
    for _ in range(limit):
        if not asyncio.run(worker.run_once()):
            return
    print("警告：后台任务在达到上限前仍未排空")


def _resolve_project(repo: Any, identifier: str | None) -> dict[str, Any]:
    projects = repo.list_projects()
    if identifier:
        for project in projects:
            if project["id"] == identifier or project["name"] == identifier:
                return project
        raise SystemExit(f"未找到项目：{identifier}")
    if projects:
        return projects[0]
    return repo.create_project("HarnessLab 演示项目", "local-dev-user")


def _print_summary(runs: list[dict[str, Any]], top_k: int) -> None:
    print(f"\n{'=' * 78}\n对照结果（固定数据集，只改一个变量）\n{'=' * 78}")
    header = f"{'变体':<22}{'模式':<9}{'Recall@' + str(top_k):<12}{'MRR':<10}{'无答案处理':<12}"
    print(header)
    print("-" * 78)
    for run in runs:
        print(
            f"{run['variant']:<22}{run['mode']:<9}{str(run['recall']):<12}"
            f"{str(run['mrr']):<10}{str(run['no_answer']):<12}"
        )


def _print_threshold_suggestion(runs: list[dict[str, Any]]) -> None:
    print(f"\n{'=' * 78}\n证据阈值（MIN_EVIDENCE_SCORE）校准\n{'=' * 78}")
    print("判定口径：证据强度 = 本次召回中最强的向量余弦相似度（跨模式可比）")
    print("要求：应命中题的最低分 > 无答案题的最高分，阈值才可能有区分能力。")
    for run in runs:
        answerable = [
            case["evidence_score"]
            for case in run["cases"]
            if case.get("expected_count", 0) > 0
        ]
        no_answer = [
            case["evidence_score"]
            for case in run["cases"]
            if case.get("expects_insufficient_evidence")
        ]
        if not answerable:
            continue
        low = min(answerable)
        high = max(no_answer) if no_answer else 0.0
        print(f"\n[{run['variant']} / {run['mode']}]")
        print(f"  应命中题最低分 {low:.4f}（{len(answerable)} 题） ｜ 无答案题最高分 {high:.4f}（{len(no_answer)} 题）")
        if not no_answer:
            print("  数据集中没有标了 expects_insufficient_evidence 的样本，无法校准阈值。")
            continue
        if low > high:
            suggestion = round(low - (low - high) / 3, 4)
            print(f"  分布可分：建议阈值 {suggestion:.4f}（取两者之间偏保守的一侧）")
        else:
            print(
                "  ⚠ 分布重叠：单一相似度阈值无法区分「可检索题」与「无答案题」。\n"
                "    结论：不要把阈值当门禁。应保持较低阈值（避免误伤可检索题），"
                "把「证据不足」判定交给系统提示中的模型指令与验收规则，"
                "并在报告中把该指标按未达标记录。"
            )


def _write_report(runs: list[dict[str, Any]], settings: Any, project_id: str) -> None:
    from harnesslab.utils import iso, new_id

    report = {
        "calibration_id": new_id()[:8],
        "created_at": iso(),
        "project_id": project_id,
        "environment": {
            "embedding_provider": settings.embedding_provider,
            "embedding_model": settings.embedding_model,
            "embedding_base_url": settings.embedding_base_url,
            "normalize": settings.embedding_normalize,
            "batch_size": settings.embedding_batch_size,
            "chat_provider": settings.chat_provider,
            "chat_model": settings.chat_model,
        },
        "notice": "数据集为自建合成资料；样本量小，结论需用更大数据集复核后才能作为发布门槛依据。",
        "runs": runs,
    }
    output = settings.artifact_dir / "evals" / f"calibration-{report['calibration_id']}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n完整对照报告：{output}")


if __name__ == "__main__":
    sys.exit(main())
