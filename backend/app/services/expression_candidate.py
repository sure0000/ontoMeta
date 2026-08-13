"""候选口径表达式：别名化字段 + 表达式体 → 可编译 AST，并**当场过编译器**。

## 为什么要有这一层

口径的权威表达是 AST（``BusinessLogic.expression_json``），而 AST 里的 ``refs`` 要求写明
``ref_id`` / ``object_name`` / ``property_name``（还有一串 id）。让模型直接产这个结构，等于
让它一次同时做对三件事：挑对字段、拼对结构、编对 id——错一个就是一条编不出来的口径，而
**此前编译器根本不在写入路径上**（``compile_metric`` 只认已落库已发布的口径），所以错误要到
建任务的 dry-run 才暴露。

这一层把那三件事拆开：

1. **字段单独声明**（``fields``：别名 → 对象.字段），服务端对着已发布本体逐条解析。
   认不出来就当场报错，并把该对象真实有哪些字段一并回给调用方——模型据此改，而不是重猜。
2. **表达式体只用别名**（``{"ref": "别名"}``），``ref_id`` 直接取别名，不做任何改写，
   于是「模型写的引用」与「AST 里的引用」不可能对不上。
3. **组装完立刻编译**（``compile_candidate``，走与已发布口径完全同一条编译+自证路径）。
   编不过就把编译器的错误码与提示原样交出去。

于是「LLM 出表达式」这件事的守卫不是提示词，而是编译器本身：编得出且过得了语义证明的
才叫一条口径，人最终看到的是**真 SQL 与口径展开轨迹**，不是一段自然语言承诺。

本模块**不写库**：产出的 AST 由调用方决定怎么用（agent 出提案、前端存草稿）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.models import EntityStatus, ObjectType, Property
from app.services.metric_compiler import (
    LOGIC_TYPES,
    CompiledMetric,
    MetricCompileError,
    compile_candidate,
)


class CandidateError(ValueError):
    """候选表达式组装失败。``detail`` 里带可照做的修正信息（候选字段清单等）。"""

    def __init__(self, code: str, message: str, detail: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}

    def to_dict(self) -> dict:
        return {"error": self.message, "code": self.code, **self.detail}


@dataclass(frozen=True)
class CandidateExpression:
    ast: dict
    compiled: CompiledMetric

    @property
    def sql(self) -> str:
        return self.compiled.sql

    @property
    def caliber_trace(self) -> list[str]:
        return list(self.compiled.caliber_trace)


def _walk_ref_ids(node: Any, out: set[str]) -> None:
    """收集表达式体里出现的全部 ``{"ref": X}``（条件可任意嵌套 and/or）。"""
    if isinstance(node, dict):
        ref = node.get("ref")
        if isinstance(ref, str):
            out.add(ref)
        for value in node.values():
            _walk_ref_ids(value, out)
    elif isinstance(node, list):
        for value in node:
            _walk_ref_ids(value, out)


def resolve_fields(db: Session, *, ontology_id: str, fields: list[dict]) -> list[dict]:
    """``[{alias, object, property?}]`` → AST 的 ``refs`` 数组（对已发布本体逐条核验）。

    ``property`` 可省（只引用对象本身，如纯计数口径的主对象）。
    """
    if not isinstance(fields, list) or not fields:
        raise CandidateError("no_fields", "需要 fields：表达式用到的本体字段（别名 → 对象.字段）")

    published = EntityStatus.PUBLISHED.value
    objects = (
        db.query(ObjectType)
        .filter(ObjectType.ontology_id == ontology_id, ObjectType.status == published)
        .all()
    )
    by_name = {(o.name or "").strip().lower(): o for o in objects}

    refs: list[dict] = []
    seen: set[str] = set()
    for item in fields:
        if not isinstance(item, dict):
            raise CandidateError("bad_field", "fields 的每一项必须是对象 {alias, object, property}")
        alias = str(item.get("alias") or "").strip()
        obj_name = str(item.get("object") or "").strip()
        prop_name = str(item.get("property") or "").strip()
        if not alias or not obj_name:
            raise CandidateError(
                "bad_field", "fields 每项都要有 alias 与 object", {"got": item}
            )
        if alias in seen:
            raise CandidateError("duplicate_alias", f"别名「{alias}」重复", {"alias": alias})
        seen.add(alias)

        obj = by_name.get(obj_name.lower())
        if obj is None:
            raise CandidateError(
                "unknown_object",
                f"对象「{obj_name}」不在该本体的已发布对象里",
                {
                    "object": obj_name,
                    # 给候选而不只是说「不存在」：模型据此改，不必再瞎猜一轮。
                    "available_objects": sorted(o.name for o in objects)[:30],
                },
            )
        ref: dict[str, Any] = {
            "ref_id": alias,
            "object_type_id": obj.id,
            "object_name": obj.name,
            "object_display_name": obj.display_name,
        }
        if prop_name:
            prop = (
                db.query(Property)
                .filter(
                    Property.object_type_id == obj.id,
                    Property.status == published,
                )
                .all()
            )
            hit = next(
                (p for p in prop if (p.name or "").strip().lower() == prop_name.lower()), None
            )
            if hit is None:
                raise CandidateError(
                    "unknown_property",
                    f"字段「{obj.display_name}.{prop_name}」不在该对象的已发布字段里",
                    {
                        "object": obj.name,
                        "property": prop_name,
                        "available_columns": sorted(p.name for p in prop)[:30],
                    },
                )
            ref.update(
                property_id=hit.id,
                property_name=hit.name,
                property_display_name=hit.display_name,
            )
        refs.append(ref)
    return refs


def build_ast(
    *, logic_type: str, refs: list[dict], body: dict
) -> dict:
    """组装 AST。表达式体里出现的引用必须全部在 fields 里声明过。"""
    lt = str(logic_type or "").strip().lower()
    if lt not in LOGIC_TYPES:
        raise CandidateError(
            "bad_logic_type",
            f"logic_type 须为 {'/'.join(LOGIC_TYPES)}，收到「{logic_type}」",
            {"available": list(LOGIC_TYPES)},
        )
    if not isinstance(body, dict) or not body:
        raise CandidateError("no_body", "需要 body：该类型的表达式体")

    declared = {str(r["ref_id"]) for r in refs}
    used: set[str] = set()
    _walk_ref_ids(body, used)
    unknown = sorted(used - declared)
    if unknown:
        # 不拦的话，未声明的引用会被 _ref_of 静默解析成 None——SUM 退化成 COUNT(*)
        # 这类「跑得通但算错」的降级，正是口径漂移最隐蔽的形态。
        raise CandidateError(
            "undeclared_ref",
            f"表达式里用到了未在 fields 声明的别名：{'、'.join(unknown)}",
            {"undeclared": unknown, "declared": sorted(declared)},
        )
    return {"type": lt, "refs": refs, "body": body}


def compile_expression(
    db: Session,
    *,
    ontology_id: str,
    logic_type: str,
    name: str,
    display_name: str | None = None,
    fields: list[dict],
    body: dict,
    summary: str | None = None,
    dialect: str | None = None,
) -> CandidateExpression:
    """全流程：解析字段 → 组装 AST → **编译并自证**。任一步失败抛 CandidateError。

    编译器的错误被翻译成同一种 ``CandidateError``，调用方（agent 工具 / API）只需
    统一把 ``to_dict()`` 交出去——里面既有原因也有可照做的修正信息。
    """
    refs = resolve_fields(db, ontology_id=ontology_id, fields=fields)
    ast = build_ast(logic_type=logic_type, refs=refs, body=body)
    try:
        compiled = compile_candidate(
            db,
            ontology_id=ontology_id,
            ast=ast,
            name=name,
            display_name=display_name or name,
            expression_summary=summary,
            limit=None,
            dialect=dialect,
        )
    except MetricCompileError as exc:
        # 编译器的 hint 就是给模型的修复信号（可用字段清单、支持的算子…），原样带出去。
        raise CandidateError(
            exc.code,
            f"表达式编译未通过：{exc.message}",
            {**(exc.hint or {}), "ast": ast},
        ) from exc
    return CandidateExpression(ast=ast, compiled=compiled)


__all__ = [
    "CandidateError",
    "CandidateExpression",
    "resolve_fields",
    "build_ast",
    "compile_expression",
]
