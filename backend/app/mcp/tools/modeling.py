"""建模工单：把一次「我要做个分析」变成可确认、可版本化的规格。

这一族此前**只有 Data Agent 一个人类入口**——REST(`/modeling-cases`) 有，前端一个页面
也没有。删掉对话模块，整套建模工单就变成没人能用的后端。

与对话里那份实现的一个实质区别：这里一律走 ``ModelingCaseService``，也就是 REST 走的那条
路。对话侧那份是**直接写表**的平行实现，写进去的需求规格字段名（`analysis_scope` /
`primary_subject` / `grain` / `time_range` / `deliverables`）与 ``RequirementSpec``
（`extra: forbid`）对不上——同一份规格，从对话建的读得出来、按 schema 校验却过不去。
迁到这里顺手把它接回唯一的那条路。
"""

from __future__ import annotations

import json
from typing import Any

from app.models.modeling import ModelingCase, ModelingCaseSpec
from app.schemas.modeling import (
    ModelingCaseCreate,
    ModelingCaseSpecConfirm,
    ModelingCaseSpecSave,
)
from app.services.modeling_case import ModelingCaseService

from . import AuthContext, ToolResult, register_tool
from ._common import as_int, loads, session

_SPEC_KINDS = ("requirement", "context", "dimensional_model", "logic_bundle", "delivery")


def _case_out(case: ModelingCase) -> dict[str, Any]:
    return {
        "case_id": case.id,
        "title": case.title,
        "primary_domain_id": case.primary_domain_id,
        "domain_ids": loads(case.domain_ids_json, []),
        "stage": case.stage,
        "current_revision": case.current_revision,
        "blocked_reason": case.blocked_reason,
        "created_at": case.created_at,
        "updated_at": case.updated_at,
    }


def _spec_out(spec: ModelingCaseSpec) -> dict[str, Any]:
    return {
        "kind": spec.kind,
        "revision": spec.revision,
        "status": spec.status,
        "payload": loads(spec.payload_json, {}),
        "content_hash": spec.content_hash,
        "validation_report": loads(spec.validation_report_json, None),
        "confirmed_by": spec.confirmed_by,
        "confirmed_at": spec.confirmed_at,
    }


@register_tool
class CreateModelingCaseTool:
    """开一张建模工单"""

    name = "create_modeling_case"
    required_role = "editor"
    description = (
        "为一次完整的分析/报表/数据应用需求开一张**建模工单**：需求 → 上下文 → 维度模型 → "
        "口径包 → 交付，每一步都是可确认、可版本化的规格。\n"
        "用户明确要做一个完整分析、或需要持续跟进的建模任务时用它；一次性问数**不要**开工单。\n"
        "建完进入需求收集阶段，用 save_modeling_spec(kind=\"requirement\") 逐步补全，"
        "补齐后 confirm_modeling_spec 推进到下一阶段。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "工单标题，一句话说清要做什么"},
            "primary_domain_id": {"type": "string", "description": "主数据域 id（可选）"},
            "domain_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "涉及的全部数据域 id（可选）",
            },
        },
        "required": ["title"],
    }

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        title = str(arguments.get("title") or "").strip()
        if not title:
            return ToolResult(success=False, error="需要 title")
        raw_domains = arguments.get("domain_ids")
        domain_ids = (
            [str(d).strip() for d in raw_domains if str(d).strip()]
            if isinstance(raw_domains, list)
            else []
        )
        try:
            with session() as db:
                case = ModelingCaseService.create(
                    db,
                    ModelingCaseCreate(
                        title=title,
                        primary_domain_id=str(arguments.get("primary_domain_id") or "").strip()
                        or None,
                        domain_ids=domain_ids,
                        owner_subject_id=auth.principal_id,
                    ),
                )
                return ToolResult(
                    success=True,
                    data=_case_out(case),
                    metadata={
                        "written": True,
                        "next_step": (
                            "用 save_modeling_spec(kind=\"requirement\") 写入业务目标、"
                            "业务过程、交付物、时间范围与验收标准。"
                        ),
                    },
                )
        except ValueError as exc:
            return ToolResult(success=False, error=str(exc))
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=f"创建建模工单失败：{exc}")


@register_tool
class GetModelingCaseTool:
    """读一张建模工单的当前状态与规格"""

    name = "get_modeling_case"
    required_role = "reader"
    description = (
        "读建模工单：当前阶段、各类规格的最新版本与确认状态。\n"
        "不给 case_id 就列最近的若干张工单（找不到 id 时先用它）。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "case_id": {"type": "string", "description": "工单 id；不给则列最近若干张"},
            "limit": {
                "type": "integer",
                "description": "列表条数上限（默认 10）",
                "default": 10,
                "minimum": 1,
                "maximum": 50,
            },
        },
    }

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        case_id = str(arguments.get("case_id") or "").strip()
        limit = as_int(arguments.get("limit"), 10, low=1, high=50)
        try:
            with session() as db:
                if not case_id:
                    rows = ModelingCaseService.list_cases(db, limit=limit)
                    return ToolResult(
                        success=True,
                        data={"cases": [_case_out(c) for c in rows]},
                        metadata={"count": len(rows)},
                    )
                case = ModelingCaseService.get(db, case_id)
                if case is None:
                    return ToolResult(success=False, error="工单不存在")
                specs = {}
                for kind in _SPEC_KINDS:
                    latest = (
                        db.query(ModelingCaseSpec)
                        .filter(
                            ModelingCaseSpec.case_id == case_id,
                            ModelingCaseSpec.kind == kind,
                        )
                        .order_by(ModelingCaseSpec.revision.desc())
                        .first()
                    )
                    if latest is not None:
                        specs[kind] = _spec_out(latest)
                return ToolResult(
                    success=True,
                    data={**_case_out(case), "specs": specs},
                    metadata={"spec_kinds": sorted(specs)},
                )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=f"读取建模工单失败：{exc}")


@register_tool
class SaveModelingSpecTool:
    """写入一份规格草稿（新版本）"""

    name = "save_modeling_spec"
    required_role = "editor"
    description = (
        "为建模工单写入一份规格草稿（自动开新 revision；内容没变则不新开）。\n"
        "**payload 按 kind 强类型校验，字段名对不上会被当场拒绝**——不要自造字段名。"
        "requirement 的字段是：business_goal（必填）、business_processes、subjects、"
        "questions、time_scope、grain_expectation、metrics、tags、refresh_sla、delivery、"
        "acceptance_criteria、open_questions。\n"
        "写完不等于定稿：要 confirm_modeling_spec 才推进阶段。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "case_id": {"type": "string", "description": "工单 id"},
            "kind": {
                "type": "string",
                "enum": list(_SPEC_KINDS),
                "description": "规格类型",
            },
            "payload": {
                "type": "object",
                "description": "规格内容（按 kind 强类型校验）",
                "additionalProperties": True,
            },
            "merge": {
                "type": "boolean",
                "description": "在最新版本上合并（默认 true）；false＝整份覆盖",
                "default": True,
            },
        },
        "required": ["case_id", "kind", "payload"],
    }

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        case_id = str(arguments.get("case_id") or "").strip()
        kind = str(arguments.get("kind") or "").strip()
        payload = arguments.get("payload")
        if not case_id or kind not in _SPEC_KINDS or not isinstance(payload, dict):
            return ToolResult(
                success=False,
                error="需要 case_id、合法的 kind 和 payload 对象",
                data={"available_kinds": list(_SPEC_KINDS)},
            )
        merge = arguments.get("merge", True)
        try:
            with session() as db:
                if merge:
                    latest = (
                        db.query(ModelingCaseSpec)
                        .filter(
                            ModelingCaseSpec.case_id == case_id,
                            ModelingCaseSpec.kind == kind,
                        )
                        .order_by(ModelingCaseSpec.revision.desc())
                        .first()
                    )
                    if latest is not None:
                        base = json.loads(latest.payload_json or "{}")
                        payload = {**base, **payload}
                spec = ModelingCaseService.save_spec(
                    db,
                    case_id,
                    kind,
                    ModelingCaseSpecSave(
                        payload=payload, proposed_by=auth.principal_name or auth.user_id
                    ),
                )
                return ToolResult(
                    success=True,
                    data=_spec_out(spec),
                    metadata={
                        "written": True,
                        # content_hash 是确认时的乐观锁：确认的必须是**被审查的那一版**。
                        "confirm_hint": (
                            "确认这一版用 confirm_modeling_spec，带上这里的 revision 与 "
                            "content_hash。"
                        ),
                    },
                )
        except ValueError as exc:
            # 强类型校验的拒绝信号原样交出去——里面写着哪个字段不对。
            return ToolResult(
                success=False, error=str(exc), metadata={"validation_error": True}
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=f"保存规格失败：{exc}")


@register_tool
class ConfirmModelingSpecTool:
    """确认一份规格，推进工单阶段"""

    name = "confirm_modeling_spec"
    # 确认是「这一版被采纳了」的表态，与制品确认同价——reviewer。
    required_role = "reviewer"
    description = (
        "确认建模工单的某一版规格并推进到下一阶段。\n"
        "**必须带 content_hash**（save_modeling_spec 返回的那一个）：它是乐观锁，"
        "保证被确认的就是被审查过的那一版；中途有人改过，确认会失败而不是悄悄确认了新内容。\n"
        "只在关键信息已明确、且用户表示确认时调用。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "case_id": {"type": "string", "description": "工单 id"},
            "kind": {"type": "string", "enum": list(_SPEC_KINDS), "description": "规格类型"},
            "revision": {"type": "integer", "description": "要确认的版本号"},
            "content_hash": {"type": "string", "description": "该版本的 content_hash（乐观锁）"},
        },
        "required": ["case_id", "kind", "revision", "content_hash"],
    }

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        case_id = str(arguments.get("case_id") or "").strip()
        kind = str(arguments.get("kind") or "").strip()
        content_hash = str(arguments.get("content_hash") or "").strip()
        revision = arguments.get("revision")
        if not case_id or kind not in _SPEC_KINDS or not content_hash:
            return ToolResult(success=False, error="需要 case_id、kind、revision、content_hash")
        try:
            revision = int(revision)
        except (TypeError, ValueError):
            return ToolResult(success=False, error="revision 必须是整数")

        try:
            with session() as db:
                spec = ModelingCaseService.confirm_spec(
                    db,
                    case_id,
                    kind,
                    revision,
                    ModelingCaseSpecConfirm(
                        confirmed_by=auth.principal_name or auth.user_id or "mcp",
                        content_hash=content_hash,
                    ),
                )
                case = ModelingCaseService.get(db, case_id)
                return ToolResult(
                    success=True,
                    data={"spec": _spec_out(spec), "case": _case_out(case) if case else None},
                    metadata={"written": True, "stage": case.stage if case else None},
                )
        except ValueError as exc:
            return ToolResult(success=False, error=str(exc))
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=f"确认规格失败：{exc}")


@register_tool
class CreateDimensionalModelTool:
    """设计一个维度模型（星型/雪花）"""

    name = "create_dimensional_model"
    required_role = "editor"
    description = (
        "基于已确认的本体与数据，设计一个维度模型（星型/雪花）：事实表带度量与维度键，"
        "维度表带代理键、自然键、属性与 SCD 策略。建完自动跑一遍校验并把 issues 回给你。\n"
        "**先定粒度再设计**：粒度说不清的维度模型，事实表迟早重建。\n"
        "确认后可编译为物化契约、生成 DDL/ETL。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "domain_id": {"type": "string", "description": "数据域 id"},
            "ontology_id": {"type": "string", "description": "本体 id"},
            "name": {"type": "string", "description": "模型标识名（snake_case）"},
            "display_name": {"type": "string", "description": "模型中文名"},
            "business_process": {"type": "string", "description": "业务过程，如「销售下单」"},
            "grain": {"type": "string", "description": "粒度，一句话说清事实表一行代表什么"},
            "fact_tables": {
                "type": "array",
                "items": {"type": "object", "additionalProperties": True},
                "description": "事实表定义（度量 + 维度键）",
            },
            "dimensions": {
                "type": "array",
                "items": {"type": "object", "additionalProperties": True},
                "description": "维度表定义（代理键、自然键、属性、SCD）",
            },
            "conformed_dimensions": {
                "type": "array",
                "items": {"type": "object", "additionalProperties": True},
                "description": "一致性维度（可选）",
            },
            "model_type": {
                "type": "string",
                "enum": ["star", "snowflake"],
                "description": "模型类型",
                "default": "star",
            },
            "modeling_case_id": {"type": "string", "description": "关联的建模工单 id（可选）"},
            "description": {"type": "string", "description": "补充说明（可选）"},
        },
        "required": [
            "domain_id",
            "ontology_id",
            "name",
            "display_name",
            "business_process",
            "grain",
            "fact_tables",
            "dimensions",
        ],
    }

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        from app.services.dimensional_model import DimensionalModelService

        service = DimensionalModelService()
        try:
            with session() as db:
                model = service.create_model(
                    db,
                    modeling_case_id=str(arguments.get("modeling_case_id") or "").strip() or None,
                    domain_id=arguments["domain_id"],
                    ontology_id=arguments["ontology_id"],
                    name=arguments["name"],
                    display_name=arguments["display_name"],
                    business_process=arguments["business_process"],
                    grain=arguments["grain"],
                    fact_tables=arguments["fact_tables"],
                    dimensions=arguments["dimensions"],
                    conformed_dimensions=arguments.get("conformed_dimensions"),
                    model_type=arguments.get("model_type", "star"),
                    description=arguments.get("description"),
                )
                validation = service.validate_model(db, model["id"])
                return ToolResult(
                    # 有校验错误时不算成功：报成 success 会被读成「模型可以用了」。
                    success=not validation.get("has_errors"),
                    data={"model": model, "validation": validation},
                    error=(
                        "维度模型有校验错误，修正后重建"
                        if validation.get("has_errors")
                        else None
                    ),
                    metadata={
                        "written": True,
                        "model_id": model["id"],
                        "issue_count": len(validation.get("issues") or []),
                    },
                )
        except KeyError as exc:
            return ToolResult(success=False, error=f"缺少必填参数：{exc}")
        except ValueError as exc:
            return ToolResult(success=False, error=str(exc))
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=f"创建维度模型失败：{exc}")
