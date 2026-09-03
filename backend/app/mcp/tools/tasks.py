"""数据任务（治理制品）查询工具集（只读）。

任务在库里就是 ``GovernanceArtifact``——同步/加工/聚合/物化四类共用一张表、一条
「草稿 → 校验 → 确认 → 执行 → 回执」流水线。查询一律走 ``AgentPipelineService``，
因为它在读时会对账 Airflow DagRun 状态：绕开它直接查表，读到的会是陈旧状态
（见 memory「制品状态靠有人读才推进」）。
"""

from __future__ import annotations

from typing import Any

from app.models.agent import ArtifactKind, ArtifactStatus
from app.services.agent_pipeline import AgentPipelineService

from . import AuthContext, ToolResult, register_tool
from ._common import as_int, loads, session

_pipeline = AgentPipelineService()

_KINDS = [k.value for k in ArtifactKind]
_STATUSES = [s.value for s in ArtifactStatus]

# 回执里对「跑成没跑成」有解释力的键。整份回执可能是几十 KB 的 DAG 文本，
# 列表里逐条塞进去会把上下文吃光。
_RECEIPT_KEYS = (
    "rows", "row_count", "dag_id", "dag_run_id", "table", "target_table",
    "message", "state", "ok", "files_written",
)


def _receipt_summary(raw: str | None) -> dict[str, Any] | None:
    data = loads(raw)
    if not isinstance(data, dict):
        return None
    picked = {k: data[k] for k in _RECEIPT_KEYS if k in data}
    if data.get("error"):
        picked["error"] = str(data["error"])[:300]
    batches = data.get("batches")
    if isinstance(batches, list) and batches:
        picked["batch_count"] = len(batches)
        # 单批时逐批展开只是把顶层的 dag_id/state 再抄一遍——多批才有信息量。
        if len(batches) > 1:
            picked["batches"] = [
                {
                    k: b[k]
                    for k in ("dag_id", "dag_run_id", "state", "ok", "error")
                    if k in b
                }
                for b in batches
                if isinstance(b, dict)
            ][:10]
    return picked or None


def _project(artifact) -> dict[str, Any]:
    spec = loads(artifact.spec_json, {})
    if not isinstance(spec, dict):
        spec = {}
    return {
        "id": artifact.id,
        "kind": artifact.kind,
        "name": artifact.name,
        "status": artifact.status,
        "ontology_id": artifact.ontology_id,
        "intent": artifact.intent,
        "is_high_risk": artifact.is_high_risk,
        "confirmed_by": artifact.confirmed_by,
        "confirmed_at": (
            artifact.confirmed_at.isoformat() if artifact.confirmed_at else None
        ),
        "executed_at": (
            artifact.executed_at.isoformat() if artifact.executed_at else None
        ),
        "created_at": artifact.created_at.isoformat() if artifact.created_at else None,
        "updated_at": artifact.updated_at.isoformat() if artifact.updated_at else None,
        # 落点与调度：回答「这任务往哪写、多久跑一次」不必再取全量 spec。
        "target": {
            k: spec[k]
            for k in (
                "target_datasource_id", "target_database", "target_table",
                "target_ods_database", "target_ods_table", "refresh_cron", "mode",
            )
            if k in spec
        },
        "receipt_summary": _receipt_summary(artifact.execution_receipt_json),
    }


@register_tool
class ListTasksTool:
    """列出数据任务"""

    name = "list_tasks"
    description = (
        "列出数据治理任务（同步 sync / 加工 transform / 聚合 metric / 物化 materialize）。"
        "可按类型、状态、本体过滤。只读；读的同时会对账 Airflow 状态，不触发执行。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "description": "任务类型",
                "enum": _KINDS,
            },
            "status": {
                "type": "string",
                "description": "制品状态",
                "enum": _STATUSES,
            },
            "ontology_id": {"type": "string", "description": "本体 ID"},
            "limit": {
                "type": "integer",
                "description": "返回条数上限（按创建时间倒序）",
                "default": 20,
                "minimum": 1,
                "maximum": 200,
            },
        },
    }

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        kind = (arguments.get("kind") or "").strip() or None
        status = (arguments.get("status") or "").strip() or None
        ontology_id = (arguments.get("ontology_id") or "").strip() or None
        if kind and kind not in _KINDS:
            return ToolResult(
                success=False,
                error=f"kind 须为 {'/'.join(_KINDS)}，收到「{kind}」",
            )
        limit = as_int(arguments.get("limit"), 20, low=1, high=200)

        try:
            with session() as db:
                rows = _pipeline.list_artifacts(
                    db, kind=kind, status=status, ontology_id=ontology_id
                )
                tasks = [_project(a) for a in rows[:limit]]
                return ToolResult(
                    success=True,
                    data={"tasks": tasks},
                    metadata={
                        "count": len(tasks),
                        "total": len(rows),
                        "truncated": len(rows) > limit,
                        "filters": {
                            "kind": kind,
                            "status": status,
                            "ontology_id": ontology_id,
                        },
                    },
                )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=f"查询任务失败：{exc}")


@register_tool
class GetTaskStatusTool:
    """查询单个任务状态"""

    name = "get_task_status"
    description = (
        "回读单个数据任务的状态、Spec、校验报告与执行回执，"
        "并尽力回读 Airflow DagRun 的实时状态（读不到就退回制品态）。只读，不触发执行。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "任务（治理制品）ID；由 list_tasks 给出",
            },
            "include_spec": {
                "type": "boolean",
                "description": "是否返回完整 Spec（可能较长）",
                "default": False,
            },
        },
        "required": ["task_id"],
    }

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        task_id = (arguments.get("task_id") or "").strip()
        if not task_id:
            return ToolResult(success=False, error="缺少 task_id")

        try:
            with session() as db:
                artifact = _pipeline.get(db, task_id)
                if artifact is None:
                    return ToolResult(success=False, error=f"任务不存在：{task_id}")

                result = _project(artifact)
                report = loads(artifact.validation_report_json)
                if report is not None:
                    result["validation_report"] = report
                if arguments.get("include_spec"):
                    result["spec"] = loads(artifact.spec_json, {})

                # 实时权威在 Airflow：尽力回读一次（从不抛异常，读不到退回制品态）。
                from app.api.agents import _try_live_state

                live = _try_live_state(db, artifact)
                if live:
                    result.update(live)

                return ToolResult(
                    success=True,
                    data=result,
                    metadata={
                        "task_id": task_id,
                        "kind": artifact.kind,
                        "state": (live or {}).get("live_state") or artifact.status,
                    },
                )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=f"查询任务状态失败：{exc}")
