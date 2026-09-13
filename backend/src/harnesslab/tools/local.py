"""本地工具实现（文档 03 第 7 节工具契约）。

工具本体只做业务动作；权限、预算、幂等与事件由工具网关统一处理。
"""

from __future__ import annotations

import ast
import math
import operator
import time
from typing import Any

from ..errors import HarnessLabError
from ..storage.models import ToolResult
from ..utils import estimate_tokens
from .base import MAX_TOOL_OUTPUT_CHARS, ToolContext

# --------------------------------------------------------------------- 计算
_ALLOWED_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_ALLOWED_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_ALLOWED_FUNCS = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sqrt": math.sqrt,
    "log": math.log,
    "ceil": math.ceil,
    "floor": math.floor,
}
_ALLOWED_NAMES = {"pi": math.pi, "e": math.e}


def _eval_node(node: ast.AST) -> Any:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise HarnessLabError("VALIDATION_ERROR", "计算表达式只允许数字常量")
    if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_BINOPS:
        return _ALLOWED_BINOPS[type(node.op)](_eval_node(node.left), _eval_node(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_UNARY:
        return _ALLOWED_UNARY[type(node.op)](_eval_node(node.operand))
    if isinstance(node, ast.Name) and node.id in _ALLOWED_NAMES:
        return _ALLOWED_NAMES[node.id]
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_FUNCS:
            raise HarnessLabError("VALIDATION_ERROR", "表达式包含不允许的函数调用")
        if node.keywords:
            raise HarnessLabError("VALIDATION_ERROR", "表达式不允许关键字参数")
        return _ALLOWED_FUNCS[node.func.id](*[_eval_node(arg) for arg in node.args])
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_eval_node(item) for item in node.elts]
    raise HarnessLabError("VALIDATION_ERROR", "表达式包含不允许的语法")


def calculator(ctx: ToolContext, expression: str) -> ToolResult:
    """受限表达式解释器：不使用 Python eval。"""
    if len(expression) > 200:
        return ToolResult(ok=False, error_code="VALIDATION_ERROR", message="表达式过长")
    try:
        tree = ast.parse(expression, mode="eval")
        value = _eval_node(tree)
    except HarnessLabError as exc:
        return ToolResult(ok=False, error_code=exc.code, message=exc.message)
    except ZeroDivisionError:
        return ToolResult(ok=False, error_code="VALIDATION_ERROR", message="除数不能为零")
    except Exception as exc:
        return ToolResult(ok=False, error_code="VALIDATION_ERROR", message=f"计算失败：{type(exc).__name__}")
    return ToolResult(
        ok=True,
        data={
            "expression": expression,
            "value": value,
            "note": "仅完成表达式求值；资料未给出的输入不得外推",
        },
    )


# --------------------------------------------------------------------- 知识
def next_citation_label(existing: list[dict[str, Any]]) -> int:
    """引用标签在全 run 内全局唯一，编号从已有引用之后继续。"""
    highest = 0
    for citation in existing:
        label = str(citation.get("label", ""))
        if label.startswith("S") and label[1:].isdigit():
            highest = max(highest, int(label[1:]))
    return highest + 1


def search_knowledge(ctx: ToolContext, query: str, top_k: int = 8) -> ToolResult:
    result = ctx.knowledge.search(
        ctx.project_id, query, top_k=max(1, min(top_k, 20)), index_version=ctx.index_version
    )
    # 引用标签跨多次检索、跨审批中断保持稳定：
    # 已引用过的 chunk 复用原标签，新 chunk 从已有最大编号之后继续编号。
    label_by_chunk = {str(c["chunk_id"]): str(c["label"]) for c in ctx.citations}
    counter = next_citation_label(ctx.citations)
    hits: list[dict[str, Any]] = []
    new_citations: list[dict[str, Any]] = []
    for hit in result.hits:
        chunk = hit.chunk
        chunk_id = str(chunk["id"])
        label = label_by_chunk.get(chunk_id)
        if label is None:
            label = f"S{counter}"
            label_by_chunk[chunk_id] = label
            counter += 1
            new_citations.append(
                {
                    "label": label,
                    "chunk_id": chunk_id,
                    "document_id": chunk["document_id"],
                    "document_version": chunk["document_version"],
                    "source_title": chunk["source_title"],
                    "heading_path": chunk["heading_path"],
                    "page": chunk.get("page"),
                    "char_start": chunk.get("char_start"),
                    "char_end": chunk.get("char_end"),
                    "snippet": chunk["text"][:400],
                    "score": round(hit.score, 6),
                }
            )
        hits.append(
            {
                "label": label,
                "chunk_id": chunk_id,
                "source_title": chunk["source_title"],
                "document_version": chunk["document_version"],
                "heading_path": chunk["heading_path"],
                "page": chunk.get("page"),
                "score": round(hit.score, 4),
                "text": chunk["text"][:600],
            }
        )
    ctx.citations = [*ctx.citations, *new_citations]
    payload = {
        "query": query,
        "mode": result.mode,
        "index_version": result.index_version,
        "evidence_score": round(result.evidence_score, 4),
        "evidence_score_kind": "vector_cosine_max",
        "insufficient_evidence": result.insufficient_evidence,
        "note": result.note,
        "citation_labels": "标签在整个运行内唯一；再次引用同一片段会复用原标签",
        "hits": hits,
    }
    return ToolResult(ok=True, data=payload, truncated=len(str(payload)) > MAX_TOOL_OUTPUT_CHARS)


def read_source(ctx: ToolContext, chunk_id: str) -> ToolResult:
    data = ctx.knowledge.read_source(chunk_id, project_id=ctx.project_id)
    return ToolResult(ok=True, data=data)


# --------------------------------------------------------------------- 产物
def list_artifacts(ctx: ToolContext) -> ToolResult:
    artifacts = ctx.repo.list_artifacts(run_id=ctx.run_id) + ctx.repo.list_artifacts(
        project_id=ctx.project_id, limit=20
    )
    seen: set[str] = set()
    items = []
    for artifact in artifacts:
        if artifact["id"] in seen:
            continue
        seen.add(artifact["id"])
        items.append(
            {
                "artifact_id": artifact["id"],
                "title": artifact["title"],
                "relative_path": artifact["relative_path"],
                "media_type": artifact["media_type"],
                "size": artifact["size"],
                "created_at": artifact["created_at"],
            }
        )
    return ToolResult(ok=True, data={"artifacts": items})


def read_artifact(ctx: ToolContext, artifact_id: str) -> ToolResult:
    artifact = ctx.repo.get_artifact(artifact_id)
    if artifact["project_id"] != ctx.project_id:
        return ToolResult(ok=False, error_code="FORBIDDEN", message="产物不属于当前项目")
    runs_root = ctx.workspace.root.parent
    workspace = type(ctx.workspace)(runs_root / artifact["run_id"])
    try:
        text = workspace.read_text(artifact["relative_path"])
    except HarnessLabError as exc:
        return ToolResult(ok=False, error_code=exc.code, message=exc.message)
    truncated = len(text) > MAX_TOOL_OUTPUT_CHARS
    return ToolResult(
        ok=True,
        data={"artifact_id": artifact_id, "title": artifact["title"], "text": text[:MAX_TOOL_OUTPUT_CHARS]},
        truncated=truncated,
    )


def write_report(ctx: ToolContext, title: str, content_markdown: str, filename: str = "") -> ToolResult:
    from ..utils import utcnow

    safe_name = filename.strip() or f"report-{utcnow().strftime('%Y%m%d-%H%M%S')}.md"
    if not safe_name.endswith(".md"):
        safe_name = f"{safe_name}.md"
    relative = f"reports/{safe_name}"
    try:
        stored_path, content_hash, size = ctx.workspace.write_text(relative, content_markdown)
    except HarnessLabError as exc:
        return ToolResult(ok=False, error_code=exc.code, message=exc.message, retryable=False)
    artifact = ctx.repo.create_artifact(
        run_id=ctx.run_id,
        project_id=ctx.project_id,
        title=title or safe_name,
        relative_path=stored_path,
        media_type="text/markdown",
        content_hash=content_hash,
        size=size,
    )
    ctx.events.emit(
        ctx.run_id,
        "artifact.created",
        {
            "artifact_id": artifact["id"],
            "title": artifact["title"],
            "relative_path": stored_path,
            "size": size,
            "media_type": "text/markdown",
        },
    )
    return ToolResult(
        ok=True,
        data={
            "artifact_id": artifact["id"],
            "title": artifact["title"],
            "relative_path": stored_path,
            "size": size,
            "download_url": f"/api/v1/artifacts/{artifact['id']}/download",
        },
        artifact_ref=artifact["id"],
    )


# --------------------------------------------------------------------- 模拟工单
def create_ticket_record(ctx: ToolContext, operation_id: str, title: str, body: str) -> dict[str, Any]:
    """真正写库的动作；由工具网关在审批通过后调用，并以操作 ID 保证不产生第二份工单。"""
    started = time.perf_counter()
    ticket = ctx.repo.create_ticket(
        project_id=ctx.project_id, title=title, body=body, tool_operation_id=operation_id
    )
    return {
        "ticket": ticket,
        "duration_ms": int((time.perf_counter() - started) * 1000),
        "idempotent": ctx.repo.ticket_by_operation(operation_id) is not None,
    }


def describe_payload(text: str) -> dict[str, int]:
    return {"chars": len(text), "estimated_tokens": estimate_tokens(text)}
