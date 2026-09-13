"""检索与引用评测（文档 07 第 2、3、9 节）。

本 Runner 只评价“检索质量 + 无答案处理”，不评价生成式措辞；
生成质量、任务成功率与故障恢复由真实模型评测单独执行并在报告中标注“未执行”。
"""

from __future__ import annotations

import platform
import sys
from pathlib import Path
from typing import Any

from ..config import REPO_ROOT, Settings, get_settings
from ..knowledge.service import KnowledgeService
from ..storage.repositories import Repository
from ..utils import iso, new_id

DEFAULT_DATASET = REPO_ROOT / "datasets" / "demo" / "gold.json"


def load_dataset(path: Path | None = None) -> dict[str, Any]:
    target = Path(path) if path else DEFAULT_DATASET
    import json

    return json.loads(target.read_text(encoding="utf-8"))


def run_eval(
    repo: Repository,
    settings: Settings | None = None,
    project_id: str | None = None,
    *,
    dataset: Path | None = None,
    mode: str | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    knowledge = KnowledgeService(repo, settings)
    data = load_dataset(dataset)
    cases = data.get("cases", [])
    index_version = None
    try:
        index_version = knowledge.index_status(project_id)["active_index_version"]
    except Exception:
        index_version = None

    case_results: list[dict[str, Any]] = []
    recall_hits = 0
    recall_total = 0
    reciprocal_ranks: list[float] = []
    no_answer_total = 0
    no_answer_ok = 0

    for case in cases:
        expected_files = {
            span.get("file") for span in case.get("expected_source_spans", []) if span.get("file")
        }
        try:
            result = knowledge.search(project_id or "", case["question"], mode=mode)
            hits = result.hits
            insufficient = result.insufficient_evidence
            evidence_score = round(result.evidence_score, 4)
            error = None
        except Exception as exc:
            hits = []
            insufficient = True
            evidence_score = 0.0
            error = f"{type(exc).__name__}: {exc}"

        matched = {hit.chunk["source_title"] for hit in hits}
        hit_count = len(expected_files & matched)
        recall_hits += hit_count
        recall_total += len(expected_files)
        first_rank = 0
        for rank, hit in enumerate(hits, start=1):
            if hit.chunk["source_title"] in expected_files:
                first_rank = rank
                break
        if expected_files:
            reciprocal_ranks.append(1.0 / first_rank if first_rank else 0.0)
        # 「无答案处理」只统计明确标注 expects_insufficient_evidence=true 的题；
        # 语料中确实写有「没有给出该保证」的题属于可检索题，不能算作证据不足。
        expects_insufficient = case.get("expects_insufficient_evidence")
        if expects_insufficient is None:
            expects_insufficient = case.get("category") == "no_answer" and not expected_files
        if expects_insufficient:
            no_answer_total += 1
            if insufficient:
                no_answer_ok += 1

        case_results.append(
            {
                "case_id": case["case_id"],
                "category": case.get("category"),
                "question": case["question"],
                "expected_files": sorted(expected_files),
                "retrieved_files": sorted(matched),
                "hit_count": hit_count,
                "expected_count": len(expected_files),
                "first_relevant_rank": first_rank or None,
                "evidence_score": evidence_score,
                "insufficient_evidence": insufficient,
                "expects_insufficient_evidence": bool(expects_insufficient),
                "acceptance_rule": case.get("acceptance_rule"),
                "generation_and_tool_metrics": "未执行",
                "error": error,
            }
        )

    metrics = {
        f"recall_at_{settings.retrieval_top_k}": round(recall_hits / recall_total, 4) if recall_total else None,
        f"mrr_at_{settings.retrieval_top_k}": (
            round(sum(reciprocal_ranks) / len(reciprocal_ranks), 4) if reciprocal_ranks else None
        ),
        "no_answer_handling": round(no_answer_ok / no_answer_total, 4) if no_answer_total else None,
        "citation_validity": "未执行（需真实模型生成的引用）",
        "task_success_rate": "未执行（需真实模型端到端运行）",
        "samples": {
            "total": len(cases),
            "recall_denominator": recall_total,
            "no_answer_denominator": no_answer_total,
        },
    }
    report = {
        "eval_id": new_id()[:8],
        "dataset_version": data.get("fixture_version"),
        "mode": mode or settings.retrieval_mode,
        "top_k": settings.retrieval_top_k,
        "min_evidence_score": settings.min_evidence_score,
        "index_version": index_version,
        "project_id": project_id,
        "created_at": iso(),
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "embedding_provider": settings.embedding_provider,
            "embedding_model": settings.embedding_model,
            "embedding_dimension": settings.embedding_dimension,
            "pipeline_version": settings.pipeline_version,
            "chat_provider": settings.chat_provider,
            "chat_model": settings.chat_model,
            "notice": "容量与延迟指标未测量，报告中不填零",
        },
        "metrics": metrics,
        "case_results": case_results,
        "notes": [
            "只有标注 expects_insufficient_evidence=true 的题进入无答案处理率；"
            "语料中写有「没有给出该保证」的题属于可检索题，参与 Recall/MRR",
            "无 expected_source_spans 的题不参与 Recall/MRR 分母",
            "证据强度按向量余弦最大值判定（RRF 融合分量级不同，不能直接套用余弦阈值）",
            "阈值需用评测集校准；分布重叠时不应把阈值当门禁",
            data.get("notice", ""),
        ],
    }
    return report
