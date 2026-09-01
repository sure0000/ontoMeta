"""字段级溯源的人工操作：冲突复核、字段钉住/取消钉住、合并报告读取。"""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from app.models import (
    BusinessLogic,
    DraftGenerationTask,
    ObjectType,
    Property,
    RelationType,
)
from app.schemas import (
    ConflictItemOut,
    MergeReportOut,
    OntologyConflictsOut,
)
from app.services.common import log_change
from app.services.ontology_merge import (
    LOGIC_FIELDS,
    OBJECT_FIELDS,
    PROPERTY_FIELDS,
    RELATION_FIELDS,
)

_ENTITY_MODELS = {
    "object_type": ObjectType,
    "property": Property,
    "relation_type": RelationType,
    "business_logic": BusinessLogic,
}

_ENTITY_FIELDS = {
    "object_type": OBJECT_FIELDS,
    "property": PROPERTY_FIELDS,
    "relation_type": RELATION_FIELDS,
    "business_logic": LOGIC_FIELDS,
}


def _loads(raw, default):
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return default


def _dumps(value):
    if not value:
        return None
    return json.dumps(value, ensure_ascii=False)


class ProvenanceService:
    """字段级溯源的读写：让人工在再生成后复核冲突、钉住/放开字段。"""

    def _resolve_entity(self, db: Session, entity_type: str, entity_id: str):
        model = _ENTITY_MODELS.get(entity_type)
        if model is None:
            raise ValueError(f"未知实体类型：{entity_type}")
        entity = db.get(model, entity_id)
        if entity is None:
            raise ValueError("实体不存在")
        return entity

    def _display(self, entity) -> str:
        return getattr(entity, "display_name", None) or getattr(entity, "name", "")

    # ------------------------------------------------------------------
    # 冲突复核
    # ------------------------------------------------------------------
    def list_conflicts(self, db: Session, ontology_id: str) -> OntologyConflictsOut:
        items: list[ConflictItemOut] = []

        def _collect(entity_type: str, rows):
            for e in rows:
                conflicts = _loads(e.conflict_json, {})
                for field, triple in conflicts.items():
                    items.append(
                        ConflictItemOut(
                            entity_type=entity_type,
                            entity_id=e.id,
                            name=getattr(e, "name", ""),
                            display_name=self._display(e),
                            field=field,
                            base=triple.get("base"),
                            ours=triple.get("ours"),
                            theirs=triple.get("theirs"),
                        )
                    )

        objects = (
            db.query(ObjectType).filter(ObjectType.ontology_id == ontology_id).all()
        )
        _collect("object_type", objects)
        object_ids = [o.id for o in objects]
        if object_ids:
            _collect(
                "property",
                db.query(Property)
                .filter(Property.object_type_id.in_(object_ids))
                .all(),
            )
        _collect(
            "relation_type",
            db.query(RelationType)
            .filter(RelationType.ontology_id == ontology_id)
            .all(),
        )
        _collect(
            "business_logic",
            db.query(BusinessLogic)
            .filter(BusinessLogic.ontology_id == ontology_id)
            .all(),
        )
        return OntologyConflictsOut(
            ontology_id=ontology_id, items=items, total=len(items)
        )

    def resolve_conflict(
        self,
        db: Session,
        entity_type: str,
        entity_id: str,
        field: str,
        resolution: str,
        operator: str | None = None,
    ):
        if resolution not in {"accept_theirs", "keep_ours"}:
            raise ValueError("resolution 必须是 accept_theirs / keep_ours")
        entity = self._resolve_entity(db, entity_type, entity_id)
        conflicts = _loads(entity.conflict_json, {})
        if field not in conflicts:
            raise ValueError("该字段当前无待复核冲突")

        overridden = set(_loads(entity.overridden_fields, []))
        if resolution == "accept_theirs":
            # 采纳上游：取机器新值，放开该字段（让机器后续继续接管）
            setattr(entity, field, conflicts[field].get("theirs"))
            overridden.discard(field)
            action_note = f"冲突采纳上游：{field}"
        else:
            # 保留我的：钉住该字段
            overridden.add(field)
            action_note = f"冲突保留人工值：{field}"

        conflicts.pop(field, None)
        entity.conflict_json = _dumps(conflicts)
        entity.overridden_fields = _dumps(sorted(overridden))
        entity.origin = "manual" if getattr(entity, "user_created", False) else (
            "machine_edited" if overridden else "machine"
        )
        log_change(db, entity_type, entity_id, "resolve_conflict", operator, action_note)
        db.commit()
        db.refresh(entity)
        return {"id": entity_id, "field": field, "resolution": resolution}

    def resolve_all_conflicts(
        self,
        db: Session,
        ontology_id: str,
        resolution: str,
        operator: str | None = None,
    ) -> dict:
        """一键解决本体下全部冲突：全部采纳上游 / 全部保留我的。"""
        conflicts = self.list_conflicts(db, ontology_id)
        count = 0
        for item in conflicts.items:
            try:
                self.resolve_conflict(
                    db, item.entity_type, item.entity_id, item.field, resolution, operator
                )
                count += 1
            except ValueError:
                continue
        return {"ontology_id": ontology_id, "resolved": count, "resolution": resolution}

    # ------------------------------------------------------------------
    # 字段钉住 / 放开
    # ------------------------------------------------------------------
    def set_pin(
        self,
        db: Session,
        entity_type: str,
        entity_id: str,
        field: str,
        pinned: bool,
        operator: str | None = None,
    ):
        fields = _ENTITY_FIELDS.get(entity_type, [])
        if field not in fields:
            raise ValueError(f"字段 {field} 不可钉住（允许：{fields}）")
        entity = self._resolve_entity(db, entity_type, entity_id)
        overridden = set(_loads(entity.overridden_fields, []))
        if pinned:
            overridden.add(field)
            note = f"钉住字段：{field}"
        else:
            overridden.discard(field)
            # 放开时清理该字段的遗留冲突（交回机器）
            conflicts = _loads(entity.conflict_json, {})
            conflicts.pop(field, None)
            entity.conflict_json = _dumps(conflicts)
            note = f"放开字段：{field}"
        entity.overridden_fields = _dumps(sorted(overridden))
        entity.origin = "manual" if getattr(entity, "user_created", False) else (
            "machine_edited" if overridden else "machine"
        )
        log_change(db, entity_type, entity_id, "pin_field", operator, note)
        db.commit()
        db.refresh(entity)
        return {"id": entity_id, "field": field, "pinned": pinned}

    # ------------------------------------------------------------------
    # 合并报告
    # ------------------------------------------------------------------
    def get_merge_report(
        self, db: Session, domain_id: str, task_id: str
    ) -> MergeReportOut:
        task = (
            db.query(DraftGenerationTask)
            .filter(
                DraftGenerationTask.id == task_id,
                DraftGenerationTask.domain_context_id == domain_id,
            )
            .first()
        )
        if not task:
            raise ValueError("Task not found")
        data = _loads(task.merge_report_json, {})
        return MergeReportOut(
            task_id=task.id,
            scope=task.scope,
            summary=data.get("summary", {}),
            object_types=data.get("object_types", {}),
            properties=data.get("properties", {}),
            relation_types=data.get("relation_types", {}),
            business_logics=data.get("business_logics", {}),
            segments=data.get("segments", {}),
        )
