"""任务提案工具集（只读）。

**提案 = 走一遍真 Drafter 派生出的 Spec + 一次规约校验，不写库、不执行。**

三条不能绕的规矩，都在这里体现：

1. **Spec 由 Drafter 派生，不由调用方直填**。ODS 落点、引擎、装载方式、命名都
   是从本体和契约推导的；表单/Agent 只给 context。绕开 Drafter 自拼一份 spec，
   执行时会被后端覆盖，提案卡上展示的就是一份假配置。
2. **缺什么当场说清，并附真实候选**。判据取 Drafter 自己声明的 ``required_context``
   （复用 ``_missing_action_context``），不在这里另抄一份键名。
3. **提案本身不写库**。本工具只返回 ``draft_payload``，落库要另调 ``draft_task``
   （见 ``tools/lifecycle.py``）——那一步起按角色收权：editor 到 draft/validate，
   confirm/execute 要 publisher。提案阶段因此可以放心预览而不产生任何副作用。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.agents import registry  # 导入即注册四类 Drafter/Executor
from app.agents.validation import is_blocking, validate_spec
from app.services.chat_bi_tool_schemas import (
    _ACTION_CONTEXT_HINT,
    _action_context_candidates,
    _missing_action_context,
    _sync_context_errors,
)

from . import AuthContext, ToolResult, register_tool
from ._common import session

# 各类任务在 context 里能给的键。Drafter 声明的必填项在 required_context，
# 这里补上它实际会读的可选项，好让调用方一次给全而不是试错。
_CONTEXT_HINTS: dict[str, dict[str, str]] = {
    "sync": {
        "ontology_id": "本体 ID（必填）",
        "object_type": "要同步的对象标识名；留空则按 intent 在本体内匹配",
        "source_datasource_id": "源数据源 ID（必填，须是启用的 business_source）",
        "target_datasource_id": "目标数仓数据源 ID（必填，须是启用的默认 Doris 仓）",
        "mode": "装载方式：full / incremental / cdc；留空取契约或 full",
        "refresh_cron": "调度 cron；留空只产手动触发的 DAG",
        "primary_keys": "主键列（数组），增量/CDC 幂等用",
        "incremental_column": "增量水位列",
        "partition_key": "分区键",
    },
    "transform": {
        "ontology_id": "本体 ID（必填）",
        "target_datasource_id": "目标数仓数据源 ID（必填）",
        "target_table": "加工产出的对象标识名；留空则按 intent 匹配",
        "refresh_cron": "调度 cron",
    },
    "metric": {
        "ontology_id": "本体 ID（必填）",
        "target_datasource_id": "目标数仓数据源 ID（必填）",
        "business_logic_id": "业务口径（指标）ID（必填，须已发布且已形式化）",
        "refresh_cron": "调度 cron",
    },
    "materialize": {
        "ontology_id": "本体 ID（必填）",
        "target_datasource_id": "目标数仓数据源 ID（必填）",
        "target_database": "目标库名（必填）",
        "selected_targets": "要物化的对象标识名数组；留空按 intent 选",
    },
}


def _schema_for(kind: str) -> dict[str, Any]:
    props: dict[str, Any] = {
        "intent": {
            "type": "string",
            "description": "自然语言任务意图，如「把客户主数据同步进数仓」",
        },
        "context": {
            "type": "object",
            "description": (
                "结构化上下文。可给的键：\n"
                + "\n".join(
                    f"- {key}: {desc}" for key, desc in _CONTEXT_HINTS[kind].items()
                )
                + "\nid 一律用 query_ontology / list_datasources 查到的真实值，不要自己编。"
            ),
        },
    }
    return {
        "type": "object",
        "properties": props,
        "required": ["intent", "context"],
    }


def _issue_payload(db: Session, kind: str, spec: dict, ontology_id: str | None) -> dict:
    issues = validate_spec(db, kind=kind, spec=spec, ontology_id=ontology_id)
    rendered = [{**i.to_dict(), "blocking": is_blocking(i)} for i in issues]
    # 阻断项排前面：一份 ERP 本体能产出上百条存量 ontology_issue，真正拦住执行的
    # 那一条不该埋在里面（与 agent_pipeline._rank_issues 同一动机）。
    rendered.sort(key=lambda i: not i["blocking"])
    return {
        "issues": rendered,
        "blocking_count": sum(1 for i in rendered if i["blocking"]),
    }


async def _propose(kind: str, arguments: dict) -> ToolResult:
    intent = str(arguments.get("intent") or "").strip()
    if not intent:
        return ToolResult(success=False, error="需要 intent（任务意图）")

    context = arguments.get("context")
    if not isinstance(context, dict):
        context = {}
    context = dict(context)
    if kind == "sync":
        # ODS 表名由 Drafter 按 ods_{数据域}_{原始表名} 固定生成。外部传入的值执行时
        # 必被覆盖，留着只会让提案展示一份假落点。
        context.pop("target_ods_table", None)

    try:
        drafter = registry.get_drafter(kind)
    except registry.UnregisteredKindError as exc:
        return ToolResult(success=False, error=str(exc))

    ontology_id = str(context.get("ontology_id") or "").strip() or None

    try:
        with session() as db:
            missing = _missing_action_context(kind, context)
            if missing:
                return ToolResult(
                    success=False,
                    error=f"提案缺少必要上下文：{'、'.join(missing)}",
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

            try:
                spec = drafter.draft(intent, context)
            except ValueError as exc:
                # Drafter 的拒绝是业务结论（「该对象没有物理源表」之类），原样回给
                # 调用方——它比任何转述都更能指出下一步该做什么。
                return ToolResult(
                    success=False, error=str(exc), metadata={"kind": kind}
                )

            validation = _issue_payload(db, kind, spec, ontology_id)

    except Exception as exc:  # noqa: BLE001
        return ToolResult(success=False, error=f"生成提案失败：{exc}")

    return ToolResult(
        success=True,
        data={
            "proposal": {
                "kind": kind,
                "name": drafter.suggested_name(intent, spec),
                "intent": intent,
                "ontology_id": ontology_id,
                "spec": spec,
            },
            "validation": validation,
            # 前端/调用方原样 POST /api/agents/draft 即可落成草稿。
            "draft_payload": {
                "kind": kind,
                "intent": intent,
                "context": context,
                "ontology_id": ontology_id,
            },
        },
        metadata={
            "kind": kind,
            "blocking_count": validation["blocking_count"],
            "note": (
                "这是提案，未写库、未执行。"
                "落成草稿请由人确认后 POST /api/agents/draft（body = draft_payload），"
                "再经 validate / confirm / execute。"
            ),
        },
    )


@register_tool
class ProposeSyncTool:
    """生成同步任务提案"""

    # 提案是只读的（不写库、不执行），但它是写侧智能体的起草环节——reader 不该能
    # 起草数据任务。与 REST「/api/agents 需 publisher」相比这里放到 editor：提案本身
    # 无副作用，真正落草稿/执行仍由人在 publisher 门控的 REST 端点完成。
    required_role = "editor"
    name = "propose_sync"
    description = (
        "生成数据同步任务提案：把源库表搬进数仓 ODS。"
        "落点恒为 ODS 库、表名 ods_{数据域}_{原表名}，不可指定。"
        "只出提案，不写库、不执行。"
    )
    input_schema = _schema_for("sync")

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        return await _propose("sync", arguments)


@register_tool
class ProposeTransformTool:
    """生成加工任务提案"""

    required_role = "editor"
    name = "propose_transform"
    description = (
        "生成数据加工（清洗/转换）任务提案：读已同步就绪的 ODS，产出加工结果表。"
        "只出提案，不写库、不执行。"
    )
    input_schema = _schema_for("transform")

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        return await _propose("transform", arguments)


@register_tool
class ProposeMaterializeTool:
    """生成物化任务提案"""

    required_role = "editor"
    name = "propose_materialize"
    description = (
        "生成本体物化任务提案：把本体对象建成物理表（只出建表 DDL，不搬数据）。"
        "人工建模、没有物理源表的对象要先物化。只出提案，不写库、不执行。"
    )
    input_schema = _schema_for("materialize")

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        return await _propose("materialize", arguments)


@register_tool
class ProposeMetricTool:
    """生成指标任务提案"""

    required_role = "editor"
    name = "propose_metric"
    description = (
        "生成指标（聚合）任务提案：按已发布的业务口径产出 ADS 结果表。"
        "只出提案，不写库、不执行。"
    )
    input_schema = _schema_for("metric")

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        return await _propose("metric", arguments)
