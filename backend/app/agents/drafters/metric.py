"""④ 指标任务 Drafter —— 「给出计算口径，智能体自动创建指标任务」。

**零新概念**：``models/logic.py`` 的 BusinessLogic + 双向绑定
（object 角色 subject/dimension/output、property 角色 input/output/filter/group）
本来就是「口径 + 表字段绑定」，``expression_formatter`` 已在做表达式编辑。
MetricSpec 只是把已有的 BusinessLogic 翻译成可物化的规格。

因此本 Drafter 不生成口径，只**挑选并结构化**已确认的口径——口径是人定的，
不该由模型编。
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from app.agents.common import require_context, resolve_spec_engine, select_by_intent
from app.agents.drafters.base import Drafter
from app.database import SessionLocal
from app.models import (
    BusinessLogic,
    BusinessLogicObjectBinding,
    BusinessLogicPropertyBinding,
    MaterializationContract,
    ObjectType,
    Property,
)
from app.models.warehouse import TargetKind
from app.services import flink_params
from app.services.metric_compiler import effective_logic_type


def _walk_refs(node: Any, out: list[str]) -> None:
    """收集一棵表达式子树里出现的全部 ``ref`` id（过滤条件可以任意嵌套 and/or）。"""
    if isinstance(node, dict):
        ref = node.get("ref")
        if isinstance(ref, str):
            out.append(ref)
        for value in node.values():
            _walk_refs(value, out)
    elif isinstance(node, list):
        for value in node:
            _walk_refs(value, out)


_LOGIC_TYPE_LABEL = {"metric": "指标", "tag": "标签", "rule": "规则"}


def _roles_from_expression(logic: BusinessLogic) -> dict[str, list[str]]:
    """形式化口径（``expression_json``）→ 与绑定表同形的角色字典。

    口径的 AST 自带 refs（对象名 + 属性名）与 body（聚合谁、按什么分组、过滤什么），
    信息量不少于绑定表。没有 AST 或结构不合法时返回空 dict——不猜。
    """
    if not logic.expression_json:
        return {}
    try:
        ast = json.loads(logic.expression_json)
    except (TypeError, ValueError):
        return {}
    if not isinstance(ast, dict):
        return {}
    refs = {
        str(r.get("ref_id")): r
        for r in (ast.get("refs") or [])
        if isinstance(r, dict) and r.get("ref_id")
    }
    if not refs:
        return {}
    body = ast.get("body") if isinstance(ast.get("body"), dict) else {}

    def names(ref_ids: list[str], key: str) -> list[str]:
        return sorted({
            str(refs[r][key]) for r in ref_ids if r in refs and refs[r].get(key)
        })

    measure_ids: list[str] = []
    _walk_refs(body.get("args") or [], measure_ids)
    group_ids: list[str] = []
    _walk_refs(body.get("group_by") or [], group_ids)
    filter_ids: list[str] = []
    _walk_refs(body.get("filter"), filter_ids)
    # 主对象 = 被聚合的那个属性所在的对象；纯计数口径（无 args）退回首个 ref。
    subject_ids = measure_ids or list(refs)[:1]
    return {
        "object_types": names(list(refs), "object_name"),
        "subject_objects": names(subject_ids, "object_name"),
        "dimension_objects": names(group_ids, "object_name"),
        "properties": names(list(refs), "property_name"),
        "group_by": names(group_ids, "property_name"),
        "filters": names(filter_ids, "property_name"),
        "inputs": names(measure_ids, "property_name"),
    }


class MetricDrafter(Drafter):
    kind = "metric"
    required_context = ("ontology_id",)

    def draft(self, intent: str, context: dict[str, Any]) -> dict[str, Any]:
        require_context(context, *self.required_context)
        ontology_id = context["ontology_id"]
        with SessionLocal() as db:
            logic = self._select_logic(
                db,
                intent,
                ontology_id,
                explicit_id=context.get("business_logic_id"),
                explicit_name=context.get("metric_name"),
            )
            if logic is None:
                raise ValueError(
                    "未在本体中找到匹配的业务逻辑；指标口径须先在「业务逻辑」中定义"
                )
            return self._spec_from_logic(db, logic, ontology_id, context)

    def _select_logic(
        self,
        db: Session,
        intent: str,
        ontology_id: str,
        *,
        explicit_id: str | None = None,
        explicit_name: str | None = None,
    ) -> BusinessLogic | None:
        """挑口径。优先级：显式 id（表单下拉给的就是 id）> 显式 name > 意图匹配。

        表单起草走 id 路径（下拉 value=BusinessLogic.id），对话/意图路径回退到
        name 精确匹配或分词意图匹配。
        """
        logics = (
            db.query(BusinessLogic)
            .filter(BusinessLogic.ontology_id == ontology_id)
            .all()
        )
        if explicit_id:
            return next((l for l in logics if l.id == explicit_id), None)
        if explicit_name:
            return next((l for l in logics if l.name == explicit_name), None)
        return select_by_intent(
            intent, logics, key=lambda l: (l.name, l.display_name, l.description)
        )

    def _spec_from_logic(
        self,
        db: Session,
        logic: BusinessLogic,
        ontology_id: str,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        obj_bindings = (
            db.query(BusinessLogicObjectBinding, ObjectType)
            .join(ObjectType, BusinessLogicObjectBinding.object_type_id == ObjectType.id)
            .filter(BusinessLogicObjectBinding.business_logic_id == logic.id)
            .all()
        )
        prop_bindings = (
            db.query(BusinessLogicPropertyBinding, Property)
            .join(Property, BusinessLogicPropertyBinding.property_id == Property.id)
            .filter(BusinessLogicPropertyBinding.business_logic_id == logic.id)
            .all()
        )
        contract = (
            db.query(MaterializationContract)
            .filter(
                MaterializationContract.ontology_id == ontology_id,
                MaterializationContract.target_kind == TargetKind.BUSINESS_LOGIC.value,
                MaterializationContract.target_id == logic.id,
            )
            .first()
        )

        # 绑定角色直接决定 SQL 结构：dimension→GROUP BY，filter→WHERE，input→聚合输入。
        by_role: dict[str, list[str]] = {}
        for binding, prop in prop_bindings:
            by_role.setdefault(binding.role, []).append(prop.name)

        roles = {
            "object_types": sorted({o.name for _, o in obj_bindings}),
            "subject_objects": sorted(
                {o.name for b, o in obj_bindings if b.role == "subject"}
            ),
            "dimension_objects": sorted(
                {o.name for b, o in obj_bindings if b.role == "dimension"}
            ),
            "properties": sorted({p.name for _, p in prop_bindings}),
            "group_by": sorted(set(by_role.get("group", []))),
            "filters": sorted(set(by_role.get("filter", []))),
            "inputs": sorted(set(by_role.get("input", []))),
        }
        # 一条绑定都没有，但口径已经形式化了 → 从 expression_json 的 refs 反推。
        # 形式化口径本身就写明了「聚合哪个对象的哪个属性、按什么分组、过滤什么」，
        # 比绑定表更权威（编译器 metric_compiler 正是照它生成 SQL）。此前只看绑定表，
        # 于是一条编译得出 SQL 的口径照样被判「未绑定主对象」，指标任务卡在校验、
        # 一次都执行不了。
        if not roles["object_types"]:
            roles.update(_roles_from_expression(logic))

        return {
            "metric_name": logic.name,
            "display_name": logic.display_name,
            "business_logic_id": logic.id,
            # 口径类型决定结果表形状与取数列（executor 的 _build_table/_compiled_sql）。
            # 三类共用这条任务链——标签落「标签取值 + 实体数」，规则落「违规行数」。
            "logic_type": effective_logic_type(logic),
            "expression": logic.expression_summary or "",
            **roles,
            # 见 sync.py 同名字段：引擎随目标数据源走，不取契约默认。
            "engine": resolve_spec_engine(db, context, contract),
            # 见 sync.py 同名字段：缺它执行器只渲染 DDL+SQL 不落库。
            "target_datasource_id": context.get("target_datasource_id") or None,
            # 表单显式选的层优先（此前一律取契约，表单里选的层被静默丢弃）。
            "target_layer": context.get("target_layer")
            or (contract.target_layer if contract else "ads"),
            "database_prefix": context.get("database_prefix"),
            "execution_mode": context.get("execution_mode") or "batch",  # P1-7: batch/streaming（metric 允许 streaming）
            # 任务级 Flink 执行参数（并行度/队列/提交目标/checkpoint/额外 -D）。
            # 只落人真填了的项——留空 = 跟随设置页默认。
            **flink_params.from_context(context),
        }

    def suggested_name(self, intent: str, spec: dict[str, Any]) -> str:
        return self.name_from_spec(spec)

    def name_from_spec(self, spec: dict[str, Any]) -> str:
        # 三类口径共用这条任务链，名字就不能一律叫「指标」——任务列表里一排「指标 · X」
        # 里混着标签和规则，看的人无从分辨自己在批准什么。
        label = _LOGIC_TYPE_LABEL.get(str(spec.get("logic_type") or "metric"), "指标")
        return f"{label} · {spec.get('display_name') or spec.get('metric_name')}"
