"""已发布本体的持续演进：从已发布版本创建修订草稿（场景 D）。

深拷贝已发布本体的对象/属性/关系/业务逻辑及其绑定到一个新的 draft 本体，
并把已发布值作为"人工权威"播种到字段级溯源：
- machine_baseline = 当前值
- overridden_fields = 全部可合并字段（视为已人工确认）
- origin = machine_edited

随后对新草稿再生成时，走三方合并：机器改动会与已发布权威值产生冲突，
交人工复核后 publish 为 version+1，形成完整的"发布→演进→再发布"闭环。
"""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from app.models import (
    BusinessLogic,
    BusinessLogicObjectBinding,
    BusinessLogicPropertyBinding,
    EntityStatus,
    ObjectType,
    Ontology,
    OntologyStatus,
    Property,
    RelationType,
)
from app.services.common import log_change
from app.services.ontology_merge import (
    LOGIC_FIELDS,
    OBJECT_FIELDS,
    PROPERTY_FIELDS,
    RELATION_FIELDS,
    relation_signature,
)


def _baseline(entity, fields: list[str]) -> str:
    return json.dumps({f: getattr(entity, f) for f in fields}, ensure_ascii=False)


def _all_pinned(fields: list[str]) -> str:
    return json.dumps(fields, ensure_ascii=False)


class OntologyRevisionService:
    """从已发布本体派生修订草稿。"""

    def create_revision_draft(
        self, db: Session, domain_id: str, operator: str | None = None
    ) -> Ontology:
        from app.services.query import OntologyQueryService

        query = OntologyQueryService()
        existing_draft = (
            db.query(Ontology)
            .filter(
                Ontology.domain_context_id == domain_id,
                Ontology.status == OntologyStatus.DRAFT.value,
            )
            .first()
        )
        if existing_draft is not None:
            raise ValueError("该数据域已存在草稿本体，请先处理或发布后再创建修订草稿")

        published = query.get_published_ontology(db, domain_id)
        if published is None:
            raise ValueError("该数据域尚无已发布本体，无法创建修订草稿")

        draft = Ontology(
            domain_context_id=domain_id,
            status=OntologyStatus.DRAFT.value,
            generated_by=operator or "revision",
            draft_revision=0,
        )
        db.add(draft)
        db.flush()

        self._clone(db, published.id, draft.id)
        log_change(
            db,
            "ontology",
            draft.id,
            "create_revision",
            operator,
            f"从已发布本体 v{published.version} 派生修订草稿",
        )
        db.commit()
        db.refresh(draft)
        return draft

    def _clone(self, db: Session, src_ontology_id: str, dst_ontology_id: str) -> None:
        obj_id_map: dict[str, str] = {}
        prop_id_map: dict[str, str] = {}

        # 对象
        src_objects = (
            db.query(ObjectType).filter(ObjectType.ontology_id == src_ontology_id).all()
        )
        for obj in src_objects:
            clone = ObjectType(
                ontology_id=dst_ontology_id,
                name=obj.name,
                display_name=obj.display_name,
                description=obj.description,
                canonical_term_id=obj.canonical_term_id,
                source_confidence=obj.source_confidence,
                source_ref=obj.source_ref,
                table_role=obj.table_role,
                role_confidence=obj.role_confidence,
                role_reason=obj.role_reason,
                status=EntityStatus.SUGGESTED.value,
                origin="machine_edited",
                machine_baseline=_baseline(obj, OBJECT_FIELDS),
                overridden_fields=_all_pinned(OBJECT_FIELDS),
                user_created=obj.user_created,
            )
            db.add(clone)
            db.flush()
            obj_id_map[obj.id] = clone.id

        # 属性
        if obj_id_map:
            src_props = (
                db.query(Property)
                .filter(Property.object_type_id.in_(list(obj_id_map.keys())))
                .all()
            )
            for prop in src_props:
                new_obj_id = obj_id_map.get(prop.object_type_id)
                if not new_obj_id:
                    continue
                clone = Property(
                    object_type_id=new_obj_id,
                    name=prop.name,
                    display_name=prop.display_name,
                    description=prop.description,
                    data_type=prop.data_type,
                    source_field_ref=prop.source_field_ref,
                    semantic_type=prop.semantic_type,
                    required=prop.required,
                    source_confidence=prop.source_confidence,
                    status=EntityStatus.SUGGESTED.value,
                    origin="machine_edited",
                    machine_baseline=_baseline(prop, PROPERTY_FIELDS),
                    overridden_fields=_all_pinned(PROPERTY_FIELDS),
                    user_created=prop.user_created,
                )
                db.add(clone)
                db.flush()
                prop_id_map[prop.id] = clone.id

        # 关系
        src_rels = (
            db.query(RelationType)
            .filter(RelationType.ontology_id == src_ontology_id)
            .all()
        )
        for rel in src_rels:
            new_src = obj_id_map.get(rel.source_object_type_id)
            new_tgt = obj_id_map.get(rel.target_object_type_id)
            if not new_src or not new_tgt:
                continue
            new_mapping = (
                obj_id_map.get(rel.mapping_object_type_id)
                if rel.mapping_object_type_id
                else None
            )
            new_obj_by_id = {v: db.get(ObjectType, v) for v in (new_src, new_tgt)}
            sig = relation_signature(
                new_obj_by_id[new_src].source_ref if new_obj_by_id[new_src] else None,
                new_obj_by_id[new_tgt].source_ref if new_obj_by_id[new_tgt] else None,
                rel.structure_type,
            )
            db.add(
                RelationType(
                    ontology_id=dst_ontology_id,
                    name=rel.name,
                    display_name=rel.display_name,
                    description=rel.description,
                    source_object_type_id=new_src,
                    target_object_type_id=new_tgt,
                    cardinality=rel.cardinality,
                    structure_type=rel.structure_type,
                    mapping_object_type_id=new_mapping,
                    source_evidence=rel.source_evidence,
                    source_confidence=rel.source_confidence,
                    source_signature=sig,
                    status=EntityStatus.SUGGESTED.value,
                    origin="machine_edited",
                    machine_baseline=_baseline(rel, RELATION_FIELDS),
                    overridden_fields=_all_pinned(RELATION_FIELDS),
                    user_created=rel.user_created,
                )
            )

        # 业务逻辑 + 绑定
        logic_id_map: dict[str, str] = {}
        src_logics = (
            db.query(BusinessLogic)
            .filter(BusinessLogic.ontology_id == src_ontology_id)
            .all()
        )
        for logic in src_logics:
            clone = BusinessLogic(
                ontology_id=dst_ontology_id,
                category_id=logic.category_id,
                name=logic.name,
                display_name=logic.display_name,
                logic_type=logic.logic_type,
                description=logic.description,
                expression_summary=logic.expression_summary,
                expression_draft=logic.expression_draft,
                expression_json=logic.expression_json,
                source_type=logic.source_type,
                source_ref=logic.source_ref,
                source_confidence=logic.source_confidence,
                status=EntityStatus.SUGGESTED.value,
                origin="machine_edited",
                machine_baseline=_baseline(logic, LOGIC_FIELDS),
                overridden_fields=_all_pinned(LOGIC_FIELDS),
                user_created=logic.user_created,
            )
            db.add(clone)
            db.flush()
            logic_id_map[logic.id] = clone.id

        if logic_id_map:
            for binding in (
                db.query(BusinessLogicObjectBinding)
                .filter(BusinessLogicObjectBinding.business_logic_id.in_(list(logic_id_map)))
                .all()
            ):
                new_obj = obj_id_map.get(binding.object_type_id)
                if not new_obj:
                    continue
                db.add(
                    BusinessLogicObjectBinding(
                        business_logic_id=logic_id_map[binding.business_logic_id],
                        object_type_id=new_obj,
                        role=binding.role,
                        source=binding.source,
                        confidence=binding.confidence,
                    )
                )
            for binding in (
                db.query(BusinessLogicPropertyBinding)
                .filter(BusinessLogicPropertyBinding.business_logic_id.in_(list(logic_id_map)))
                .all()
            ):
                new_prop = prop_id_map.get(binding.property_id)
                if not new_prop:
                    continue
                db.add(
                    BusinessLogicPropertyBinding(
                        business_logic_id=logic_id_map[binding.business_logic_id],
                        property_id=new_prop,
                        role=binding.role,
                        source=binding.source,
                        confidence=binding.confidence,
                    )
                )
