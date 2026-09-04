"""治理任务生命周期 MCP 工具。

工具只负责参数规范化、结果投影和异步派发；状态机、校验闸门与执行回执仍由
``AgentPipelineService`` 统一维护。
"""

from __future__ import annotations

from typing import Any

from app.api.deps import agent_pipeline
from app.jobs.artifact_execution_worker import spawn_artifact_execution_worker
from app.models.agent import ArtifactKind, ArtifactStatus
from app.services.agent_pipeline import PipelineError
from app.services.chat_bi_tool_schemas import (
    _ACTION_CONTEXT_HINT,
    _action_context_candidates,
    _missing_action_context,
    _sync_context_errors,
)

from . import AuthContext, ToolResult, register_tool
from ._common import loads, session

_KINDS = [kind.value for kind in ArtifactKind]


def _validation(artifact) -> dict[str, Any]:
    report = loads(artifact.validation_report_json, {})
    return report if isinstance(report, dict) else {}


def _task_payload(artifact) -> dict[str, Any]:
    return {
        "task_id": artifact.id,
        "status": artifact.status,
        "name": artifact.name,
        "kind": artifact.kind,
        "ontology_id": artifact.ontology_id,
        "validation": _validation(artifact),
    }


def _task_id(arguments: dict) -> str:
    return str(arguments.get("task_id") or "").strip()


@register_tool
class DraftTaskTool:
    name = "draft_task"
    required_role = "editor"
    description = (
        "把 propose_* 返回的 draft_payload 落成治理任务并立即校验。"
        "只写治理草稿并做 dry-run，不确认、不执行数仓变更。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "description": "任务类型",
                "enum": _KINDS,
            },
            "intent": {"type": "string", "description": "任务意图"},
            "context": {
                "type": "object",
                "description": "propose_* 返回的 draft_payload.context，须使用真实对象和数据源 ID",
            },
            "ontology_id": {
                "type": "string",
                "description": "任务所属本体 ID，须与 context.ontology_id 一致",
            },
        },
        "required": ["kind", "intent", "context", "ontology_id"],
    }

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        kind = str(arguments.get("kind") or "").strip()
        intent = str(arguments.get("intent") or "").strip()
        context_arg = arguments.get("context")
        ontology_id = str(arguments.get("ontology_id") or "").strip()
        if kind not in _KINDS:
            return ToolResult(
                success=False, error=f"kind 须为 {'/'.join(_KINDS)}，收到「{kind}」"
            )
        if not intent:
            return ToolResult(success=False, error="需要 intent（任务意图）")
        if not isinstance(context_arg, dict):
            return ToolResult(success=False, error="context 必须是对象")
        if not ontology_id:
            return ToolResult(success=False, error="缺少 ontology_id")

        context = dict(context_arg)
        context_ontology_id = str(context.get("ontology_id") or "").strip()
        if context_ontology_id and context_ontology_id != ontology_id:
            return ToolResult(
                success=False,
                error=(
                    "ontology_id 与 context.ontology_id 不一致；"
                    "请直接使用 propose_* 返回的完整 draft_payload"
                ),
            )
        context["ontology_id"] = ontology_id
        if kind == "sync":
            context.pop("target_ods_table", None)

        try:
            with session() as db:
                missing = _missing_action_context(kind, context)
                if missing:
                    return ToolResult(
                        success=False,
                        error=f"起草缺少必要上下文：{'、'.join(missing)}",
                        data={
                            "missing": missing,
                            "hint": _ACTION_CONTEXT_HINT,
                            **_action_context_candidates(db, missing),
                        },
                        metadata={"kind": kind},
                    )
                if kind == "sync":
                    errors = _sync_context_errors(db, context, ontology_id=ontology_id)
                    if errors:
                        return ToolResult(
                            success=False,
                            error="；".join(errors),
                            metadata={"kind": kind},
                        )

                artifact = agent_pipeline.draft(
                    db,
                    kind=kind,
                    intent=intent,
                    context=context,
                    ontology_id=ontology_id,
                    user_created=True,
                )
                try:
                    artifact = agent_pipeline.validate(db, artifact.id)
                except Exception as exc:  # 草稿已提交，必须把可恢复的 id 告诉调用方
                    db.refresh(artifact)
                    return ToolResult(
                        success=False,
                        error=f"草稿已创建，但自动校验失败：{exc}",
                        data=_task_payload(artifact),
                        metadata={"kind": kind, "partial_success": True},
                    )
                return ToolResult(
                    success=True,
                    data=_task_payload(artifact),
                    metadata={
                        "kind": kind,
                        "blocking_count": _validation(artifact).get(
                            "blocking_count", 0
                        ),
                    },
                )
        except (PipelineError, LookupError, ValueError) as exc:
            return ToolResult(success=False, error=str(exc), metadata={"kind": kind})
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=f"起草任务失败：{exc}")


@register_tool
class ValidateTaskTool:
    name = "validate_task"
    required_role = "editor"
    description = "重跑治理任务的校验闸门与 dry-run；不确认、不执行。"
    input_schema = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "治理任务 ID"},
        },
        "required": ["task_id"],
    }

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        task_id = _task_id(arguments)
        if not task_id:
            return ToolResult(success=False, error="缺少 task_id")
        try:
            with session() as db:
                artifact = agent_pipeline.validate(db, task_id)
                return ToolResult(success=True, data=_task_payload(artifact))
        except (PipelineError, LookupError, ValueError) as exc:
            return ToolResult(success=False, error=str(exc))
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=f"校验任务失败：{exc}")


@register_tool
class ConfirmTaskTool:
    name = "confirm_task"
    required_role = "publisher"
    description = (
        "确认一个已通过校验的治理任务。publisher 令牌代表调用方已获执行授权；"
        "本工具只确认，不触发执行。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "治理任务 ID"},
        },
        "required": ["task_id"],
    }

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        task_id = _task_id(arguments)
        if not task_id:
            return ToolResult(success=False, error="缺少 task_id")
        operator = (
            auth.principal_name
            or auth.principal_id
            or auth.user_id
            or f"{auth.client_type}:{auth.role}"
        )
        try:
            with session() as db:
                artifact = agent_pipeline.confirm(db, task_id, operator=operator)
                return ToolResult(
                    success=True,
                    data={
                        "task_id": artifact.id,
                        "status": artifact.status,
                        "confirmed_by": artifact.confirmed_by,
                    },
                )
        except PipelineError as exc:
            return ToolResult(
                success=False,
                error=f"确认失败：{exc}；请先解决校验阻断项并重新 validate_task",
            )
        except (LookupError, ValueError) as exc:
            return ToolResult(success=False, error=str(exc))
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=f"确认任务失败：{exc}")


@register_tool
class ExecuteTaskTool:
    name = "execute_task"
    required_role = "publisher"
    description = (
        "异步执行一个已确认的治理任务并立即返回。返回成功只表示已受理；"
        "最终结果必须用 get_task_status 轮询。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "治理任务 ID"},
        },
        "required": ["task_id"],
    }

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        task_id = _task_id(arguments)
        if not task_id:
            return ToolResult(success=False, error="缺少 task_id")
        try:
            with session() as db:
                artifact, claimed = agent_pipeline.claim_execution(db, task_id)
                if not claimed:
                    return ToolResult(
                        success=True,
                        data={
                            "task_id": artifact.id,
                            "status": artifact.status,
                            "accepted": artifact.status
                            == ArtifactStatus.EXECUTING.value,
                            "already_running": artifact.status
                            == ArtifactStatus.EXECUTING.value,
                            "already_succeeded": artifact.status
                            == ArtifactStatus.SUCCEEDED.value,
                            "note": "用 get_task_status 查询执行回执与终态",
                        },
                    )
                try:
                    spawn_artifact_execution_worker(artifact.id)
                except Exception:
                    agent_pipeline.release_execution_claim(db, artifact.id)
                    raise
                return ToolResult(
                    success=True,
                    data={
                        "task_id": artifact.id,
                        "status": ArtifactStatus.EXECUTING.value,
                        "accepted": True,
                        "note": "执行已受理；用 get_task_status 轮询终态",
                    },
                    metadata={"async": True, "outcome": "accepted"},
                )
        except (PipelineError, LookupError, ValueError) as exc:
            return ToolResult(success=False, error=str(exc))
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=f"派发执行任务失败：{exc}")
