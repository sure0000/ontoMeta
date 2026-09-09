"""Business-logic management tools.

``authoring.py`` handles expression compilation and creation.  This module
exposes the remaining Web workflow: categories, bindings, publish review and
human-confirmed publication.
"""

from __future__ import annotations

import json
from typing import Any

from app.models import (
    BusinessLogic,
    BusinessLogicCategory,
    EntityStatus,
)
from app.schemas import ConfirmationCreate
from app.services.edit import EditService
from app.services.logic_query import OntologyQueryService
from app.services.metric_compiler import MetricCompileError, compile_candidate
from app.services.publish import ConfirmationService

from . import AuthContext, ToolResult, register_tool
from ._common import dump, host_confirmation_gate, session

_query = OntologyQueryService()
_edit = EditService()
_confirmations = ConfirmationService()
_OBJECT_ROLES = ("subject", "dimension", "output")
_PROPERTY_ROLES = ("input", "output", "filter", "group")


def _logic_payload(db, logic_id: str) -> dict[str, Any] | None:
    detail = _query.get_business_logic(db, logic_id)
    if detail is None:
        return None
    keys = (
        "id", "name", "display_name", "logic_type", "status", "description",
        "expression_summary", "expression_draft", "expression_json", "source_type",
        "source_ref", "domain_context_id", "domain_name", "ontology_id",
        "category_id", "category_name", "bound_object_count", "bound_property_count",
        "landing", "updated_at",
    )
    payload: dict[str, Any] = {}
    for key in keys:
        value = getattr(detail, key, None)
        # Pydantic models nested in a detail (landing/bindings) need the same
        # JSON-safe conversion as the rest of the MCP response, but the large
        # available_* candidate lists are intentionally never touched.
        payload[key] = dump(value) if key == "landing" else value
    payload["object_bindings"] = [dump(item) for item in (getattr(detail, "object_bindings", None) or [])]
    payload["property_bindings"] = [dump(item) for item in (getattr(detail, "property_bindings", None) or [])]
    payload["formalized"] = bool(getattr(detail, "expression_json", None))
    return payload


def _review_digest(logic: BusinessLogic, compiled: Any, issues: list[dict[str, Any]]) -> str:
    from ._common import artifact_approval_digest

    payload = {
        "logic_id": logic.id,
        "name": logic.name,
        "status": logic.status,
        "expression_json": json.loads(logic.expression_json or "null"),
        "object_bindings": sorted(
            (binding.object_type_id, binding.role) for binding in logic.object_bindings
        ),
        "property_bindings": sorted(
            (binding.property_id, binding.role) for binding in logic.property_bindings
        ),
        "compiled_sql": getattr(compiled, "sql", None),
        "issues": issues,
    }
    # Reuse the same canonical hash function semantics without pretending a
    # BusinessLogic is a GovernanceArtifact.
    return artifact_approval_digest(
        type(
            "_DigestObject",
            (),
            {
                "id": logic.id,
                "kind": "business_logic",
                "ontology_id": logic.ontology_id,
                "spec_json": json.dumps(payload, ensure_ascii=False),
                "validation_report_json": json.dumps(issues, ensure_ascii=False),
            },
        )()
    )


def _compile_stored_logic(db, logic: BusinessLogic):
    if not logic.expression_json:
        return None, {"code": "no_expression", "message": "该口径只有文字定义，尚未形式化"}
    try:
        ast = json.loads(logic.expression_json)
    except (TypeError, ValueError):
        return None, {"code": "bad_expression", "message": "expression_json 不是合法表达式"}
    try:
        return compile_candidate(
            db,
            ontology_id=logic.ontology_id,
            ast=ast,
            name=logic.name,
            display_name=logic.display_name,
            expression_summary=logic.expression_summary,
        ), None
    except MetricCompileError as exc:
        return None, {"code": exc.code, "message": str(exc), "detail": exc.detail}
    except Exception as exc:  # noqa: BLE001
        return None, {"code": "compile_failed", "message": str(exc)}


@register_tool
class ListLogicCategoriesTool:
    name = "list_logic_categories"
    display_name = "口径分类目录"
    category = "logic"
    required_role = "reader"
    description = "列出业务逻辑分类及每类数量；create_logic/update_logic 的 category_id 必须来自此目录。"
    input_schema = {"type": "object", "properties": {}}

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        try:
            with session() as db:
                rows = db.query(BusinessLogicCategory).order_by(BusinessLogicCategory.name.asc()).all()
                data = [
                    {
                        "id": row.id,
                        "name": row.name,
                        "description": row.description,
                        "logic_count": db.query(BusinessLogic).filter(BusinessLogic.category_id == row.id).count(),
                    }
                    for row in rows
                ]
            return ToolResult(success=True, data={"categories": data}, metadata={"count": len(data)})
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=f"读取业务逻辑分类失败：{exc}")


@register_tool
class UpdateLogicTool:
    name = "update_logic"
    display_name = "编辑口径"
    category = "logic"
    required_role = "editor"
    description = "编辑业务逻辑的名称、说明、类型或分类；表达式必须使用 update_logic_expression 编译后更新。"
    input_schema = {
        "type": "object",
        "properties": {
            "logic_id": {"type": "string"},
            "display_name": {"type": "string"},
            "description": {"type": "string"},
            "logic_type": {"type": "string", "enum": ["metric", "tag", "rule"]},
            "expression_summary": {"type": "string"},
            "category_id": {"type": "string", "description": "分类 ID；传空字符串表示取消分类"},
        },
        "required": ["logic_id"],
    }

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        logic_id = str(arguments.get("logic_id") or "").strip()
        if not logic_id:
            return ToolResult(success=False, error="缺少 logic_id")
        allowed = {"display_name", "description", "logic_type", "expression_summary", "category_id"}
        changes = {key: arguments[key] for key in allowed if key in arguments}
        if not changes:
            return ToolResult(success=False, error="至少提供一个要修改的字段")
        try:
            with session() as db:
                if "category_id" in changes and changes["category_id"]:
                    if db.get(BusinessLogicCategory, str(changes["category_id"])) is None:
                        return ToolResult(success=False, error="category_id 不存在")
                _edit.update_business_logic(
                    db,
                    logic_id,
                    operator=auth.principal_name or auth.principal_id,
                    **changes,
                )
                payload = _logic_payload(db, logic_id)
            return ToolResult(success=True, data={"logic": payload}, metadata={"written": True, "logic_id": logic_id})
        except ValueError as exc:
            return ToolResult(success=False, error=str(exc))
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=f"更新业务逻辑失败：{exc}")


@register_tool
class BindLogicObjectTool:
    name = "bind_logic_object"
    display_name = "绑定口径对象"
    category = "logic"
    required_role = "editor"
    description = "把同一本体中的真实业务对象绑定到口径；对象 ID 必须来自 query_objects/query_object_detail。"
    input_schema = {"type": "object", "properties": {"logic_id": {"type": "string"}, "object_type_id": {"type": "string"}, "role": {"type": "string", "enum": list(_OBJECT_ROLES)}}, "required": ["logic_id", "object_type_id"]}

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        logic_id = str(arguments.get("logic_id") or "").strip()
        object_id = str(arguments.get("object_type_id") or "").strip()
        role = str(arguments.get("role") or "subject").strip()
        if not logic_id or not object_id:
            return ToolResult(success=False, error="需要 logic_id 和 object_type_id")
        try:
            with session() as db:
                binding = _edit.bind_object_to_logic(db, logic_id, object_id, role=role, operator=auth.principal_name or auth.principal_id)
                return ToolResult(success=True, data={"binding": dump(binding)}, metadata={"written": True})
        except ValueError as exc:
            return ToolResult(success=False, error=str(exc))
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=f"绑定业务逻辑对象失败：{exc}")


@register_tool
class UnbindLogicObjectTool:
    name = "unbind_logic_object"
    display_name = "解绑口径对象"
    category = "logic"
    required_role = "editor"
    description = "解除一条业务逻辑对象绑定；只影响绑定关系，不删除对象或口径。"
    input_schema = {"type": "object", "properties": {"binding_id": {"type": "string"}}, "required": ["binding_id"]}

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        binding_id = str(arguments.get("binding_id") or "").strip()
        if not binding_id:
            return ToolResult(success=False, error="缺少 binding_id")
        try:
            with session() as db:
                result = _edit.unbind_object_from_logic(db, binding_id, operator=auth.principal_name or auth.principal_id)
            return ToolResult(success=True, data=result, metadata={"written": True})
        except ValueError as exc:
            return ToolResult(success=False, error=str(exc))


@register_tool
class BindLogicPropertyTool:
    name = "bind_logic_property"
    display_name = "绑定口径字段"
    category = "logic"
    required_role = "editor"
    description = "把同一本体中的真实字段绑定到口径；property_id 必须来自 query_object_detail。"
    input_schema = {"type": "object", "properties": {"logic_id": {"type": "string"}, "property_id": {"type": "string"}, "role": {"type": "string", "enum": list(_PROPERTY_ROLES)}}, "required": ["logic_id", "property_id"]}

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        logic_id = str(arguments.get("logic_id") or "").strip()
        property_id = str(arguments.get("property_id") or "").strip()
        role = str(arguments.get("role") or "input").strip()
        if not logic_id or not property_id:
            return ToolResult(success=False, error="需要 logic_id 和 property_id")
        try:
            with session() as db:
                binding = _edit.bind_property_to_logic(db, logic_id, property_id, role=role, operator=auth.principal_name or auth.principal_id)
                return ToolResult(success=True, data={"binding": dump(binding)}, metadata={"written": True})
        except ValueError as exc:
            return ToolResult(success=False, error=str(exc))
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=f"绑定业务逻辑字段失败：{exc}")


@register_tool
class UnbindLogicPropertyTool:
    name = "unbind_logic_property"
    display_name = "解绑口径字段"
    category = "logic"
    required_role = "editor"
    description = "解除一条业务逻辑字段绑定；只影响绑定关系，不删除字段或口径。"
    input_schema = {"type": "object", "properties": {"binding_id": {"type": "string"}}, "required": ["binding_id"]}

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        binding_id = str(arguments.get("binding_id") or "").strip()
        if not binding_id:
            return ToolResult(success=False, error="缺少 binding_id")
        try:
            with session() as db:
                result = _edit.unbind_property_from_logic(db, binding_id, operator=auth.principal_name or auth.principal_id)
            return ToolResult(success=True, data=result, metadata={"written": True})
        except ValueError as exc:
            return ToolResult(success=False, error=str(exc))


@register_tool
class ReviewLogicPublishTool:
    name = "review_logic_publish"
    display_name = "口径发布审查"
    category = "logic"
    required_role = "reader"
    description = "审查业务逻辑是否具备发布条件，编译已存表达式并返回警告、compiled_sql 要点和确认 digest；不发布、不写库。"
    input_schema = {"type": "object", "properties": {"logic_id": {"type": "string"}}, "required": ["logic_id"]}

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        logic_id = str(arguments.get("logic_id") or "").strip()
        if not logic_id:
            return ToolResult(success=False, error="缺少 logic_id")
        with session() as db:
            logic = db.get(BusinessLogic, logic_id)
            if logic is None or logic.deleted_by_user:
                return ToolResult(success=False, error="业务逻辑不存在")
            issues: list[dict[str, Any]] = []
            compiled, compile_issue = _compile_stored_logic(db, logic)
            if compile_issue and compile_issue.get("code") != "no_expression":
                issues.append({"blocking": True, **compile_issue})
            elif compile_issue:
                issues.append({"blocking": False, **compile_issue})
            if logic.status == EntityStatus.PUBLISHED.value:
                issues.append({"blocking": False, "code": "already_published", "message": "该口径已经发布"})
            payload = _logic_payload(db, logic_id) or {}
            digest = _review_digest(logic, compiled, issues)
            return ToolResult(
                success=True,
                data={
                    "logic": payload,
                    "ready": not any(item.get("blocking") for item in issues) and logic.status != EntityStatus.PUBLISHED.value,
                    "issues": issues,
                    "compiled_sql": getattr(compiled, "sql", None),
                    "caliber_trace": getattr(compiled, "caliber_trace", None),
                    "confirmation_digest": digest,
                },
                metadata={"written": False, "blocking_count": sum(bool(item.get("blocking")) for item in issues)},
            )


@register_tool
class PublishLogicTool:
    name = "publish_logic"
    display_name = "发布口径"
    category = "logic"
    required_role = "publisher"
    description = "发布已通过 review_logic_publish 的业务逻辑。必须传同一 confirmation_digest 和宿主 ask_user_question 的批准；否则只返回阻断。"
    input_schema = {"type": "object", "properties": {"logic_id": {"type": "string"}, "confirmation_digest": {"type": "string"}, "host_confirmation": {"type": "object"}}, "required": ["logic_id", "confirmation_digest"]}

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        logic_id = str(arguments.get("logic_id") or "").strip()
        supplied_digest = str(arguments.get("confirmation_digest") or "").strip()
        if not logic_id or not supplied_digest:
            return ToolResult(success=False, error="需要 logic_id 和 confirmation_digest")
        try:
            with session() as db:
                logic = db.get(BusinessLogic, logic_id)
                if logic is None:
                    return ToolResult(success=False, error="业务逻辑不存在")
                issues: list[dict[str, Any]] = []
                compiled, compile_issue = _compile_stored_logic(db, logic)
                if compile_issue and compile_issue.get("code") != "no_expression":
                    issues.append({"blocking": True, **compile_issue})
                elif compile_issue:
                    issues.append({"blocking": False, **compile_issue})
                digest = _review_digest(logic, compiled, issues)
                if supplied_digest != digest:
                    return ToolResult(success=False, error="confirmation_digest 已过期，请重新 review_logic_publish", data={"current_digest": digest}, metadata={"gate": "review_stale"})
                if any(item.get("blocking") for item in issues):
                    return ToolResult(success=False, error="业务逻辑未通过发布审查", data={"issues": issues}, metadata={"gate": "publish_review"})
                blocked = host_confirmation_gate(db, auth, arguments.get("host_confirmation"), digest, action="业务逻辑发布")
                if blocked is not None:
                    blocked.data = {"logic_id": logic_id, "confirmation_digest": digest, "issues": issues}
                    return blocked
                operator = auth.principal_name or auth.principal_id or auth.user_id
                confirmation = _confirmations.create(
                    db,
                    ConfirmationCreate(
                        ontology_id=logic.ontology_id,
                        target_type="business_logic",
                        target_id=logic.id,
                        action_type="publish",
                        operator=operator,
                        reason="MCP 宿主已确认业务逻辑发布审查",
                        payload={"confirmation_digest": digest},
                    ),
                )
                confirmed = _confirmations.confirm(db, confirmation.id, operator=operator)
                payload = _logic_payload(db, logic_id)
                return ToolResult(success=True, data={"logic": payload, "confirmation_id": confirmed.id, "confirmation_status": confirmed.confirmation_status}, metadata={"written": True, "approval_digest": digest})
        except ValueError as exc:
            return ToolResult(success=False, error=str(exc))
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=f"发布业务逻辑失败：{exc}")


__all__ = [
    "ListLogicCategoriesTool", "UpdateLogicTool", "BindLogicObjectTool",
    "UnbindLogicObjectTool", "BindLogicPropertyTool", "UnbindLogicPropertyTool",
    "ReviewLogicPublishTool", "PublishLogicTool",
]
