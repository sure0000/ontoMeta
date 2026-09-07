"""口径创作：编译表达式、建口径、规约自检。

Web 上能手工建一条口径，但「用自然语言说清算法 → 组装成本体引用 → 编译成真 SQL → 当场
自证」这条路此前只长在旧对话入口里，所以这里把它做成
工具。

**敢让模型写表达式，是因为守卫不是提示词而是编译器**：组装不出、编译不过、或过不了语义
证明的 AST 根本到不了落库那一步，编译器的错误码与可用字段会原样回给调用方去改。

写与不写分成两个工具，不是一个带 ``dry_run`` 的开关：
``compile_logic_expression`` 是 reader，任何人都能拿它试到编过为止；``create_logic`` /
``update_logic_expression`` 是 editor，才真的落库。这与「提案 vs 确认」是同一条边界——
只是它由角色和工具边界承担，而不是由前端那颗按钮（见 STATUS 的 DR-2）。
"""

from __future__ import annotations

import re
from typing import Any

from app.models import BusinessLogicCategory, DomainContext, Ontology, OntologyStatus
from app.services.metric_compiler import COMPARE_OPS, LOGIC_TYPES, METRIC_OPS

from . import AuthContext, ToolResult, register_tool
from ._common import dump, session

_TYPE_LABEL = {"metric": "指标", "tag": "标签", "rule": "规则"}

_FIELDS_SCHEMA: dict[str, Any] = {
    "type": "array",
    "description": (
        "表达式用到的本体字段。别名由你起（表达式体里用它引用），"
        "object/property 必须是本体里的**技术名**（如 order / amount），不是中文显示名。"
    ),
    "items": {
        "type": "object",
        "properties": {
            "alias": {"type": "string", "description": "别名，如 amt"},
            "object": {"type": "string", "description": "对象技术名，如 order"},
            "property": {
                "type": "string",
                "description": "字段技术名，如 amount；只引用对象本身时可省",
            },
        },
        "required": ["alias", "object"],
    },
}

_BODY_DESCRIPTION = (
    "表达式体，按类型三选一，引用一律写 {\"ref\":\"别名\"}：\n"
    "· metric：{\"operation\":\"" + "|".join(METRIC_OPS) + "\","
    "\"args\":[{\"ref\":\"amt\"}],\"group_by\":[{\"ref\":\"st\"}],\"filter\":<条件|null>}"
    "（SUM/AVG 只能作用于语义类型为 measure 的字段）\n"
    "· tag：{\"cases\":[{\"when\":<条件>,\"then\":{\"value\":\"大额\"}},"
    "{\"when\":null,\"then\":{\"value\":\"普通\"}}]}"
    "（when=null 即 else 分支；**每个分支都要给标签值**，否则编不过）\n"
    "· rule：{\"condition\":<应当成立的条件>,\"message\":\"违规说明\"}"
    "（规则统计的是**不满足**该条件的行数）\n"
    "条件形如 {\"left\":{\"ref\":\"amt\"},\"op\":\"" + "|".join(COMPARE_OPS)
    + "\",\"right\":{\"value\":1000}}，可嵌套 {\"op\":\"and|or\",\"conditions\":[…]}"
)

_BODY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": _BODY_DESCRIPTION,
    "additionalProperties": True,
}


def _slug(display_name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_]+", "_", display_name).strip("_").lower()
    return slug or "new_logic"


def _categories(db) -> list[dict[str, str]]:
    return [
        {"id": c.id, "name": c.name}
        for c in db.query(BusinessLogicCategory).order_by(BusinessLogicCategory.name.asc()).all()
    ]


def _resolve_ontology(db, *, ontology_id: str | None, domain_id: str | None):
    """定位一条已发布本体。两个入参都给了要一致，都不给就在唯一已发布本体上兜底。

    绝不「随手挑一个」：挑错本体的后果是口径挂到别的域上，而调用方看不出来。
    """
    if ontology_id:
        onto = db.get(Ontology, ontology_id)
        if onto is None:
            return None, "本体不存在"
        if onto.status != OntologyStatus.PUBLISHED.value:
            return None, "本体尚未发布，不能在其上建口径"
        if domain_id and onto.domain_context_id != domain_id:
            return None, "ontology_id 与 domain_id 不属于同一个数据域"
        return onto, None
    if domain_id:
        onto = (
            db.query(Ontology)
            .filter(
                Ontology.domain_context_id == domain_id,
                Ontology.status == OntologyStatus.PUBLISHED.value,
            )
            .first()
        )
        if onto is None:
            return None, "该数据域没有已发布本体"
        return onto, None
    published = (
        db.query(Ontology).filter(Ontology.status == OntologyStatus.PUBLISHED.value).all()
    )
    if not published:
        return None, "当前没有已发布本体"
    if len(published) > 1:
        names = {
            o.id: (db.get(DomainContext, o.domain_context_id).name if o.domain_context_id else "")
            for o in published
        }
        return None, (
            "有多个已发布本体，必须指定 ontology_id 或 domain_id：\n"
            + "\n".join(f"- {oid}（{name}）" for oid, name in names.items())
        )
    return published[0], None


@register_tool
class CompileLogicExpressionTool:
    """把口径表达式编译成 SQL（只编不写）"""

    name = "compile_logic_expression"
    required_role = "reader"
    description = (
        "把一条口径的表达式**编译成真 SQL** 并自证，不写库。\n"
        "编不过会回 `code` + 可用字段/支持的算子，照着改再编一次；编过了回 `compiled_sql` "
        "和口径展开轨迹 `caliber_trace`——那是给人看的凭据。\n"
        "**字段必须来自本体**：先 query_objects / query_object_detail 确认对象名与字段技术名，"
        "别猜。要落库用 create_logic（新建）或 update_logic_expression（补全已有口径）。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "display_name": {"type": "string", "description": "口径中文名，如「大额订单」"},
            "logic_type": {
                "type": "string",
                "enum": list(LOGIC_TYPES),
                "description": "指标 metric / 标签 tag / 规则 rule",
            },
            "fields": _FIELDS_SCHEMA,
            "body": _BODY_SCHEMA,
            "name": {
                "type": "string",
                "description": "英文标识符（snake_case）；缺省由中文名派生",
            },
            "summary": {"type": "string", "description": "口径的一句话说明（给人看）"},
            "ontology_id": {"type": "string", "description": "在哪个本体上编译"},
            "domain_id": {"type": "string", "description": "或给数据域，由服务端取其已发布本体"},
        },
        "required": ["display_name", "logic_type", "fields", "body"],
    }

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        with session() as db:
            payload, error = _compile(db, arguments)
            if error is not None:
                return error
            return ToolResult(
                success=True,
                data=payload,
                metadata={"compiled": True, "written": False},
            )


def _compile(db, arguments: dict) -> tuple[dict[str, Any], ToolResult | None]:
    """编译一条候选表达式。返回 (载荷, 错误 ToolResult 或 None)。"""
    from app.services.expression_candidate import CandidateError, compile_expression

    display_name = str(arguments.get("display_name") or "").strip()
    if not display_name:
        return {}, ToolResult(success=False, error="需要 display_name（口径中文名）")
    logic_type = str(arguments.get("logic_type") or "").strip().lower()
    if logic_type not in LOGIC_TYPES:
        return {}, ToolResult(
            success=False,
            error=f"logic_type 须为 {'/'.join(LOGIC_TYPES)}，收到「{logic_type}」",
            data={"available": list(LOGIC_TYPES)},
        )
    onto, why = _resolve_ontology(
        db,
        ontology_id=str(arguments.get("ontology_id") or "").strip() or None,
        domain_id=str(arguments.get("domain_id") or "").strip() or None,
    )
    if onto is None:
        return {}, ToolResult(success=False, error=why)

    name = str(arguments.get("name") or "").strip() or _slug(display_name)
    summary = str(arguments.get("summary") or "").strip()
    try:
        candidate = compile_expression(
            db,
            ontology_id=onto.id,
            logic_type=logic_type,
            name=name,
            display_name=display_name,
            fields=arguments.get("fields") or [],
            body=arguments.get("body") or {},
            summary=summary or None,
        )
    except CandidateError as exc:
        # 编译器的拒绝信号原样交出去：里面有可用字段/支持的算子/该怎么修。
        return {}, ToolResult(
            success=False,
            error=exc.message,
            data=exc.to_dict(),
            metadata={"code": exc.code, "validation_error": True},
        )
    return (
        {
            "ontology_id": onto.id,
            "domain_id": onto.domain_context_id,
            "logic_type": logic_type,
            "name": name,
            "display_name": display_name,
            "summary": summary,
            "compiled_sql": candidate.sql,
            "caliber_trace": candidate.caliber_trace,
            "expression_json": candidate.ast,
        },
        None,
    )


@register_tool
class CreateLogicTool:
    """新建一条业务口径（指标 / 标签 / 规则）"""

    name = "create_logic"
    # 落库动作。editor 是「能改、不能发布」那一层——口径建出来是草稿，发布仍走既有闸门。
    required_role = "editor"
    description = (
        "新建一条业务口径（指标 metric / 标签 tag / 规则 rule）**草稿**。建出来是草稿，"
        "发布仍走既有治理闸门。\n"
        "两种用法：\n"
        "· 只给名字与口径说明（`description` 必填，那是这条口径唯一承载含义的地方）；\n"
        "· 连表达式一起给（`fields` + `body`）——服务端先编译并自证，编不过就不建，"
        "把原因回给你改。**建议先用 compile_logic_expression 编到通过再来建。**\n"
        "建前先 search_logics 查重：同名口径重复建出来，后面没人分得清该用哪条。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "display_name": {"type": "string", "description": "口径中文名"},
            "logic_type": {
                "type": "string",
                "enum": list(LOGIC_TYPES),
                "description": "指标 metric / 标签 tag / 规则 rule",
            },
            "description": {
                "type": "string",
                "description": "这条口径怎么算，一句话说清（不给表达式时必填）",
            },
            "fields": _FIELDS_SCHEMA,
            "body": _BODY_SCHEMA,
            "name": {"type": "string", "description": "英文标识符；缺省由中文名派生"},
            "category_id": {
                "type": "string",
                "description": "业务逻辑分类 id（须来自目录；不填＝未分类）",
            },
            "ontology_id": {"type": "string", "description": "在哪个本体上建"},
            "domain_id": {"type": "string", "description": "或给数据域"},
        },
        "required": ["display_name", "logic_type"],
    }

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        from app.services.edit import EditService

        has_expression = bool(arguments.get("fields")) and bool(arguments.get("body"))
        description = str(arguments.get("description") or "").strip()
        if not has_expression and not description:
            return ToolResult(
                success=False,
                error=(
                    "需要 description：这条口径怎么算，一句话说清"
                    "（不带表达式时，它是唯一承载口径含义的字段）"
                ),
                metadata={"hint": "例如「客户分组表中 is_group=0（非分组节点）的分组数量」"},
            )

        with session() as db:
            compiled: dict[str, Any] = {}
            if has_expression:
                compiled, error = _compile(db, arguments)
                if error is not None:
                    return error
                onto_id, domain_id = compiled["ontology_id"], compiled["domain_id"]
                name, display_name = compiled["name"], compiled["display_name"]
                logic_type = compiled["logic_type"]
            else:
                onto, why = _resolve_ontology(
                    db,
                    ontology_id=str(arguments.get("ontology_id") or "").strip() or None,
                    domain_id=str(arguments.get("domain_id") or "").strip() or None,
                )
                if onto is None:
                    return ToolResult(success=False, error=why)
                onto_id, domain_id = onto.id, onto.domain_context_id
                display_name = str(arguments.get("display_name") or "").strip()
                logic_type = str(arguments.get("logic_type") or "").strip().lower()
                if logic_type not in LOGIC_TYPES:
                    return ToolResult(
                        success=False,
                        error=f"logic_type 须为 {'/'.join(LOGIC_TYPES)}",
                        data={"available": list(LOGIC_TYPES)},
                    )
                name = str(arguments.get("name") or "").strip() or _slug(display_name)

            category_id = str(arguments.get("category_id") or "").strip() or None
            options = _categories(db)
            if category_id and category_id not in {c["id"] for c in options}:
                return ToolResult(
                    success=False,
                    error="category_id 不属于当前业务逻辑分类目录",
                    data={"category_options": options},
                )
            try:
                detail = EditService().create_business_logic(
                    db,
                    domain_id=domain_id,
                    name=name,
                    display_name=display_name,
                    logic_type=logic_type,
                    description=description or compiled.get("summary") or None,
                    expression_summary=(
                        description or compiled.get("summary") or display_name
                    ),
                    expression_json=compiled.get("expression_json"),
                    category_id=category_id,
                    operator=auth.principal_name or auth.user_id,
                )
            except ValueError as exc:
                return ToolResult(success=False, error=str(exc))

            label = _TYPE_LABEL.get(logic_type, logic_type)
            return ToolResult(
                success=True,
                data={
                    "logic": dump(detail),
                    "compiled_sql": compiled.get("compiled_sql"),
                    "caliber_trace": compiled.get("caliber_trace"),
                },
                metadata={
                    "written": True,
                    "ontology_id": onto_id,
                    "formalized": bool(compiled),
                    "summary": f"已建{label}草稿「{display_name}」"
                    + ("（表达式已编译通过）" if compiled else "（表达式待补）"),
                },
            )


@register_tool
class UpdateLogicExpressionTool:
    """给已有口径补全表达式"""

    name = "update_logic_expression"
    required_role = "editor"
    description = (
        "为**已存在**的口径补全/替换表达式（search_logics 查得到、但还没形式化的那些）。"
        "同样先编译自证，编不过不写。\n"
        "要新建一条口径用 create_logic。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "logic_id": {"type": "string", "description": "要补全的口径 id"},
            "fields": _FIELDS_SCHEMA,
            "body": _BODY_SCHEMA,
            "summary": {"type": "string", "description": "口径的一句话说明（给人看）"},
            "category_id": {"type": "string", "description": "业务逻辑分类 id（可选）"},
        },
        "required": ["logic_id", "fields", "body"],
    }

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        from app.models import BusinessLogic
        from app.services.edit import EditService

        logic_id = str(arguments.get("logic_id") or "").strip()
        if not logic_id:
            return ToolResult(success=False, error="需要 logic_id")

        with session() as db:
            logic = db.get(BusinessLogic, logic_id)
            if logic is None:
                return ToolResult(success=False, error="口径不存在")
            # 表达式必须在**这条口径自己所属的本体**上编译。拿别的本体去编，字段名可能
            # 恰好也存在，编得过、SQL 却指向另一个域的表。
            merged = {
                **arguments,
                "display_name": logic.display_name or logic.name,
                "logic_type": logic.logic_type,
                "name": logic.name,
                "ontology_id": logic.ontology_id,
            }
            compiled, error = _compile(db, merged)
            if error is not None:
                return error

            category_id = str(arguments.get("category_id") or "").strip() or None
            if category_id and category_id not in {c["id"] for c in _categories(db)}:
                return ToolResult(
                    success=False,
                    error="category_id 不属于当前业务逻辑分类目录",
                    data={"category_options": _categories(db)},
                )
            summary = str(arguments.get("summary") or "").strip()
            try:
                detail = EditService().update_business_logic(
                    db,
                    logic_id,
                    logic_type=logic.logic_type,
                    expression_summary=summary or logic.expression_summary or logic.display_name,
                    expression_json=compiled["expression_json"],
                    category_id=category_id,
                    operator=auth.principal_name or auth.user_id,
                )
            except ValueError as exc:
                return ToolResult(success=False, error=str(exc))
            return ToolResult(
                success=True,
                data={
                    "logic": dump(detail),
                    "compiled_sql": compiled["compiled_sql"],
                    "caliber_trace": compiled["caliber_trace"],
                },
                metadata={"written": True, "logic_id": logic_id},
            )


@register_tool
class LintSpecTool:
    """用当前治理规约自检一份建数/建表规格"""

    name = "lint_spec"
    required_role = "reader"
    description = (
        "用**当前生效的数据治理规约**自检一份建数/建表规格：提任务前调，照返回的 `fix` 自己改，"
        "别等治理闸门打回。\n"
        "主要查物理表名等命名规约；返回违规项列表，空列表＝本次检查未发现问题。\n"
        "**注意 `compliant` 可能是 null**：spec 里没有物理表名时一条规则都没跑过，"
        "那时不得说「符合治理规约」。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "description": "制品类型：sync / transform / materialize / metric",
            },
            "spec": {
                "type": "object",
                "description": "要自检的 Spec（至少带 target_table 才有可检查的条款）",
                "additionalProperties": True,
            },
        },
        "required": ["kind", "spec"],
    }

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        from app.governance import lint_against_standard

        spec = arguments.get("spec")
        if not isinstance(spec, dict):
            return ToolResult(success=False, error="需要 spec 对象")
        kind = str(arguments.get("kind") or "").strip()

        with session() as db:
            violations = lint_against_standard(kind, spec, db)
        # Spec 层可校验的条款只有物理标识符命名：没有 target_table 的 spec 一条规则都没
        # 跑过，此时回一句「合规」会被直接转述成「已通过治理规范校验」——一个空洞的通过。
        checked = bool(str(spec.get("target_table") or "").strip())
        data: dict[str, Any] = {
            "violations": violations,
            "compliant": (not violations) if checked else None,
            "checked_rules": ["naming_snake_case", "naming_reserved_word"] if checked else [],
        }
        if not checked:
            data["note"] = (
                "本次没有校验任何条款：Spec 层只能校验物理标识符（target_table）命名，"
                "该 spec 里没有物理表名。不得据此声称「符合治理规约」。"
            )
        return ToolResult(
            success=True,
            data=data,
            metadata={"kind": kind, "violation_count": len(violations)},
        )
