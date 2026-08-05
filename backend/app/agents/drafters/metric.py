"""④ 指标任务 Drafter —— 「给出计算口径，智能体自动创建指标任务」。

**零新概念**：``models/logic.py`` 的 BusinessLogic + 双向绑定
（object 角色 subject/dimension/output、property 角色 input/output/filter/group）
本来就是「口径 + 表字段绑定」，``expression_formatter`` 已在做表达式编辑。
MetricSpec 只是把已有的 BusinessLogic 翻译成可物化的规格。

因此本 Drafter 不生成口径，只**挑选并结构化**已确认的口径——口径是人定的，
不该由模型编。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.agents.common import require_context, select_by_intent
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


class MetricDrafter(Drafter):
    kind = "metric"
    required_context = ("ontology_id",)

    def draft(self, intent: str, context: dict[str, Any]) -> dict[str, Any]:
        require_context(context, *self.required_context)
        ontology_id = context["ontology_id"]
        with SessionLocal() as db:
            logic = self._select_logic(db, intent, ontology_id, context.get("metric_name"))
            if logic is None:
                raise ValueError(
                    "未在本体中找到匹配的业务逻辑；指标口径须先在「业务逻辑」中定义"
                )
            return self._spec_from_logic(db, logic, ontology_id, context)

    def _select_logic(
        self, db: Session, intent: str, ontology_id: str, explicit: str | None
    ) -> BusinessLogic | None:
        logics = (
            db.query(BusinessLogic)
            .filter(BusinessLogic.ontology_id == ontology_id)
            .all()
        )
        if explicit:
            return next((l for l in logics if l.name == explicit), None)
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

        return {
            "metric_name": logic.name,
            "display_name": logic.display_name,
            "business_logic_id": logic.id,
            "expression": logic.expression_summary or "",
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
            "engine": context.get("engine") or (contract.engines[0] if contract and contract.engines else "hive"),
            "target_layer": contract.target_layer if contract else "ads",
            "database_prefix": context.get("database_prefix"),
        }

    def suggested_name(self, intent: str, spec: dict[str, Any]) -> str:
        return f"指标 · {spec.get('display_name') or spec.get('metric_name')}"
