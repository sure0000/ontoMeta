"""数据任务（治理制品）查询工具集（只读）。

任务在库里就是 ``GovernanceArtifact``——同步/加工/聚合/物化四类共用一张表、一条
「草稿 → 校验 → 确认 → 执行 → 回执」流水线。查询一律走 ``AgentPipelineService``，
因为它在读时会对账 Airflow DagRun 状态：绕开它直接查表，读到的会是陈旧状态
（见 memory「制品状态靠有人读才推进」）。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from app.models.agent import ArtifactKind, ArtifactStatus
from app.services.agent_pipeline import AgentPipelineService

from . import AuthContext, ToolResult, register_tool
from ._common import artifact_approval_digest, as_int, loads, session

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


def _read_task_status(task_id: str, *, include_spec: bool = False) -> ToolResult:
    """Read one task and reconcile its remote run once.

    Kept separate from the MCP handler so ``wait_task_status`` can long-poll without
    recursively going through the server authorization/audit pipeline.
    """
    try:
        with session() as db:
            artifact = _pipeline.get(db, task_id)
            if artifact is None:
                return ToolResult(success=False, error=f"任务不存在：{task_id}")

            result = _project(artifact)
            report = loads(artifact.validation_report_json)
            if report is not None:
                result["validation_report"] = report
            report_dict = report if isinstance(report, dict) else {}
            try:
                blocking_count = int(report_dict.get("blocking_count") or 0)
            except (TypeError, ValueError):
                blocking_count = 1
            result["interactive_approval"] = {
                "task_id": artifact.id,
                "digest": artifact_approval_digest(artifact),
                "eligible": artifact.status
                in {
                    ArtifactStatus.VALIDATED.value,
                    ArtifactStatus.CONFIRMED.value,
                }
                and blocking_count == 0,
                "instruction": (
                    "仅当 MCP 宿主已向人展示本任务方案并得到明确批准时，"
                    "才把此 digest 作为 confirm_task/execute_task 的 host_confirmation 传回"
                ),
            }
            if include_spec:
                result["spec"] = loads(artifact.spec_json, {})

            # Airflow/Doris is the runtime authority; failures here fall back to the
            # persisted artifact state and remain visible in the regular status shape.
            from app.api.agents import _try_live_state

            live = _try_live_state(db, artifact)
            if live:
                result.update(live)
            state = (live or {}).get("live_state") or artifact.status
            return ToolResult(
                success=True,
                data=result,
                metadata={"task_id": task_id, "kind": artifact.kind, "state": state},
            )
    except Exception as exc:  # noqa: BLE001
        return ToolResult(success=False, error=f"查询任务状态失败：{exc}")


@register_tool
class ListTasksTool:
    """列出数据任务"""

    name = "list_tasks"
    description = (
        "列出数据治理任务（同步 sync / 加工 transform / 聚合 metric / 物化 materialize）。"
        "可按类型、状态、本体过滤。只读，不触发执行。\n"
        "**默认不对账 Airflow**（`reconcile=false`）：列目录、找 task_id 用默认值即可，"
        "快得多。返回的 `status` 是制品自身的记录，可能落后于远端。\n"
        "要终态请对**单条**用 get_task_status（它会回读 DagRun）；"
        "确实需要整批终态时才传 `reconcile=true`——那是一次远程往返/条。"
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
            "reconcile": {
                "type": "boolean",
                "description": (
                    "是否逐条回读 Airflow DagRun 把终态回写（每条一次远程往返，很慢）。"
                    "默认 false：只读制品自身记录"
                ),
                "default": False,
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
        reconcile = bool(arguments.get("reconcile", False))

        try:
            with session() as db:
                # 多取一条只为判断是否截断——不必为了算 total 把全表拉回来（更不必对账全表）。
                rows = _pipeline.list_artifacts(
                    db,
                    kind=kind,
                    status=status,
                    ontology_id=ontology_id,
                    limit=limit + 1,
                    reconcile=reconcile,
                )
                truncated = len(rows) > limit
                tasks = [_project(a) for a in rows[:limit]]
                return ToolResult(
                    success=True,
                    data={"tasks": tasks},
                    metadata={
                        "count": len(tasks),
                        "truncated": truncated,
                        "reconciled": reconcile,
                        # 说破，免得把「制品自陈状态」当成远端终态报出去。
                        "status_note": (
                            "status 已按 Airflow DagRun 对账"
                            if reconcile
                            else "status 是制品自身记录，未回读远端；要终态用 get_task_status"
                        ),
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
        "本工具用于单次快照；异步执行后的持续追踪请用 wait_task_status。"
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
        return _read_task_status(
            task_id, include_spec=bool(arguments.get("include_spec"))
        )


@register_tool
class WaitTaskStatusTool:
    """服务端长轮询任务状态，避免客户端高频重复调用。"""

    name = "wait_task_status"
    description = (
        "在服务端等待一个任务的远端状态变化或终态，再返回与 get_task_status 相同的真实回执。"
        "只读、不触发执行；适合异步 execute_task 后追踪，避免客户端用 Bash/sleep 高频轮询。"
        "默认等待终态最多 50 秒，超时会返回当前状态并建议稍后再次等待。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "治理任务 ID"},
            "timeout_seconds": {
                "type": "number",
                "description": "服务端最长等待秒数（1-50，默认 50）",
                "default": 50,
                "minimum": 1,
                "maximum": 50,
            },
            "poll_interval_seconds": {
                "type": "number",
                "description": "服务端检查间隔秒数（1-15，默认 5）",
                "default": 5,
                "minimum": 1,
                "maximum": 15,
            },
            "until": {
                "type": "string",
                "description": "等待条件：terminal=终态（默认），change=状态变化或终态",
                "enum": ["terminal", "change"],
                "default": "terminal",
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
        task_id = str(arguments.get("task_id") or "").strip()
        if not task_id:
            return ToolResult(success=False, error="缺少 task_id")

        def _number(name: str, default: float, low: float, high: float) -> float:
            try:
                value = float(arguments.get(name, default))
            except (TypeError, ValueError):
                value = default
            return max(low, min(high, value))

        timeout = _number("timeout_seconds", 50, 1, 50)
        interval = _number("poll_interval_seconds", 5, 1, 15)
        until = str(arguments.get("until") or "terminal").strip().lower()
        if until not in {"terminal", "change"}:
            return ToolResult(success=False, error="until 须为 terminal 或 change")
        include_spec = bool(arguments.get("include_spec"))
        started = time.monotonic()
        first = _read_task_status(task_id, include_spec=include_spec)
        if not first.success:
            return first
        first_state = (first.metadata or {}).get("state")

        def _done(result: ToolResult) -> bool:
            state = (result.metadata or {}).get("state")
            terminal = bool((result.data or {}).get("terminal")) or state in {
                ArtifactStatus.SUCCEEDED.value,
                ArtifactStatus.FAILED.value,
            }
            return terminal or (until == "change" and state != first_state)

        if _done(first):
            first.metadata.update(
                {"waited_seconds": 0, "timed_out": False, "wait_condition": until}
            )
            return first

        while time.monotonic() - started < timeout:
            remaining = timeout - (time.monotonic() - started)
            await asyncio.sleep(min(interval, max(0.0, remaining)))
            result = _read_task_status(task_id, include_spec=include_spec)
            if not result.success:
                return result
            if _done(result):
                result.metadata.update(
                    {
                        "waited_seconds": round(time.monotonic() - started, 3),
                        "timed_out": False,
                        "wait_condition": until,
                    }
                )
                return result

        result = _read_task_status(task_id, include_spec=include_spec)
        if not result.success:
            return result
        result.metadata.update(
            {
                "waited_seconds": round(time.monotonic() - started, 3),
                "timed_out": True,
                "wait_condition": until,
                "next_poll_after_seconds": interval,
                "status_note": "等待超时；任务仍未到达等待条件，不能判定执行成功",
            }
        )
        return result
