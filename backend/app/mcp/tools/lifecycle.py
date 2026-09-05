"""治理任务生命周期 MCP 工具。

工具只负责参数规范化、结果投影和异步派发；状态机、校验闸门与执行回执仍由
``AgentPipelineService`` 统一维护。
"""

from __future__ import annotations

from typing import Any

from app.api.deps import agent_pipeline
from app.jobs.artifact_execution_worker import spawn_artifact_execution_worker
from app.models.agent import ArtifactKind, ArtifactStatus, GovernanceArtifact
from app.services.agent_pipeline import PipelineError
from app.services.chat_bi_tool_schemas import (
    _ACTION_CONTEXT_HINT,
    _action_context_candidates,
    _missing_action_context,
    _sync_context_errors,
)

from . import AuthContext, ToolResult, register_tool
from ._common import artifact_approval_digest, loads, session

_KINDS = [kind.value for kind in ArtifactKind]


def _execution_approval_gate(
    db, artifact, arguments: dict, auth: AuthContext
) -> tuple[str | None, ToolResult | None]:
    """代执行授权闸：与角色**正交**的第二道闸，只挡 MCP 这条路。

    角色回答的是「这个身份能不能做这类事」，发一次长期有效。而「这一条任务现在
    可以让外部 agent 自己确认、并推到远端 Airflow 真跑」是另一个决定——此前没有
    人来做这个决定：一个 publisher 令牌加一句话就够了，审计里那几条真实的远端
    执行就是这么来的。

    默认授权只能从 REST（人在界面上点）写；管理员显式开启时，本机 stdio 宿主也可用
    ask_user_question 的确认断言放行，但不会写入逐条 REST 授权字段。
    """
    from app.services.settings_service import SettingsService

    runtime = SettingsService().get_mcp_runtime(db)
    if not runtime.mcp_require_execution_approval:
        return "gate_disabled", None
    if getattr(artifact, "agent_execution_approved", False):
        return "task_approval", None

    host = arguments.get("host_confirmation")
    if host is not None:
        if not isinstance(host, dict):
            return None, ToolResult(
                success=False,
                error="host_confirmation 必须是对象",
                metadata={"gate": "host_interactive_approval"},
            )
        if not runtime.mcp_allow_stdio_interactive_approval:
            return None, ToolResult(
                success=False,
                error=(
                    "本部署未启用本机 MCP 宿主交互确认；"
                    "请由管理员在 MCP 设置中显式开启，或在任务详情逐条授权"
                ),
                metadata={"gate": "host_interactive_approval_disabled"},
            )
        if not auth.is_local_mcp or not auth.principal_id:
            return None, ToolResult(
                success=False,
                error=(
                    "宿主交互确认只接受本机 stdio 的真实 Principal；"
                    "远程 HTTP、匿名默认角色和 Admin bootstrap token 不可使用"
                ),
                metadata={"gate": "host_interactive_approval_not_allowed"},
            )
        if host.get("approved") is not True or host.get("channel") != "ask_user_question":
            return None, ToolResult(
                success=False,
                error="宿主交互确认必须来自 ask_user_question 且明确 approved=true",
                metadata={"gate": "host_interactive_approval_invalid"},
            )
        expected = artifact_approval_digest(artifact)
        supplied = str(host.get("digest") or "").strip()
        if not supplied or supplied != expected:
            return None, ToolResult(
                success=False,
                error="任务方案或校验结果已变化，旧的宿主交互确认已失效；请重新展示并确认",
                data={"task_id": artifact.id, "current_digest": expected},
                metadata={"gate": "host_interactive_approval_stale"},
            )
        return "stdio_host_interactive", None

    return None, ToolResult(
        success=False,
        error=(
            "这条任务还没有被授权代执行。角色够不代表这一条可以自动跑——"
            "请人在「任务详情」里点「允许 Agent 代执行」，然后再调一次。"
        ),
        data={
            "task_id": artifact.id,
            "status": artifact.status,
            "agent_execution_approved": False,
            "how_to_approve": (
                "前端任务详情抽屉 → 允许 Agent 代执行；"
                "或在已启用本机宿主交互确认的 dsh 中，用 ask_user_question 得到人类批准后，"
                "把 get_task_status 返回的 digest 作为 host_confirmation 传回"
            ),
            # 说清楚这不是角色问题，否则模型会去换令牌、重试、或报成「权限不足」。
            "note": "这不是角色不足（denied），换令牌没用；缺的是针对这一条任务的人工授权。",
        },
        metadata={"gate": "agent_execution_approval"},
    )


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
        "确认一个已通过校验的治理任务。本工具只确认，不触发执行。\n"
        "除 publisher 角色外还要**这一条任务被人工授权代执行**——"
        "角色是长期许可，代执行授权是逐条给的；被闸住时返回的不是权限不足，"
        "换令牌没用。也可在管理员显式启用后，由本机 stdio 宿主用 ask_user_question"
        "取得人类确认，并回传 get_task_status 给出的任务 digest。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "治理任务 ID"},
            "host_confirmation": {
                "type": "object",
                "description": (
                    "可选的本机宿主人类确认断言；仅在部署显式开启时可用。"
                    "digest 必须来自刚刚的 get_task_status，任务内容变化后自动失效"
                ),
                "properties": {
                    "approved": {"type": "boolean", "const": True},
                    "channel": {
                        "type": "string",
                        "enum": ["ask_user_question"],
                    },
                    "digest": {"type": "string"},
                },
                "required": ["approved", "channel", "digest"],
                "additionalProperties": False,
            },
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
                existing = db.get(GovernanceArtifact, task_id)
                if existing is None:
                    return ToolResult(success=False, error=f"任务不存在：{task_id}")
                approval_source, blocked = _execution_approval_gate(
                    db, existing, arguments, auth
                )
                if blocked is not None:
                    return blocked
                artifact = agent_pipeline.confirm(db, task_id, operator=operator)
                return ToolResult(
                    success=True,
                    data={
                        "task_id": artifact.id,
                        "status": artifact.status,
                        "confirmed_by": artifact.confirmed_by,
                        "approval_source": approval_source,
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
        "最终结果必须用 wait_task_status 等待终态。\n"
        "与 confirm_task 同一道代执行授权闸：授权可能在两步之间被撤销，这里会再查一次。"
        "本机宿主交互模式下须再次传入同一个任务 digest。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "治理任务 ID"},
            "host_confirmation": ConfirmTaskTool.input_schema["properties"][
                "host_confirmation"
            ],
        },
        "required": ["task_id"],
    }

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        task_id = _task_id(arguments)
        if not task_id:
            return ToolResult(success=False, error="缺少 task_id")
        try:
            with session() as db:
                existing = db.get(GovernanceArtifact, task_id)
                if existing is None:
                    return ToolResult(success=False, error=f"任务不存在：{task_id}")
                # confirm 已经过一次闸，这里再过一次：授权可以在两步之间被撤销，
                # 而真正打到远端的是这一步。
                approval_source, blocked = _execution_approval_gate(
                    db, existing, arguments, auth
                )
                if blocked is not None:
                    return blocked
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
                            "note": "用 wait_task_status 等待执行回执与终态",
                            "approval_source": approval_source,
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
                        "note": "执行已受理；用 wait_task_status 等待终态",
                        "approval_source": approval_source,
                    },
                    metadata={"async": True, "outcome": "accepted"},
                )
        except (PipelineError, LookupError, ValueError) as exc:
            return ToolResult(success=False, error=str(exc))
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=f"派发执行任务失败：{exc}")
