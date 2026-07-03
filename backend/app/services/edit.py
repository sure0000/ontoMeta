from sqlalchemy.orm import Session

from app.models import (
    BusinessLogic,
    BusinessLogicObjectBinding,
    BusinessLogicPropertyBinding,
    DomainContext,
    EntityStatus,
    ObjectType,
    Ontology,
    Property,
    RelationType,
)
from app.services.relation_terms import compact_relation_term, validate_relation_term
from app.schemas import (
    BusinessLogicDetail,
    BusinessLogicObjectBindingOut,
    BusinessLogicPropertyBindingOut,
    BusinessLogicOut,
    ObjectTypeDetail,
    ObjectTypeSummary,
    PropertyOut,
    RelationTypeOut,
)

_OBJECT_BINDING_ROLES = {"subject", "dimension", "output"}
_PROPERTY_BINDING_ROLES = {"input", "output", "filter", "group"}


def _log_change(
    db: Session,
    entity_type: str,
    entity_id: str,
    action: str,
    operator: str | None = None,
    summary: str | None = None,
) -> None:
    from app.models import EntityChangeLog

    db.add(
        EntityChangeLog(
            entity_type=entity_type,
            entity_id=entity_id,
            action=action,
            operator=operator,
            change_summary=summary,
        )
    )


class EditService:
    """工作区本体编辑与预发布。"""

    def update_object_type(
        self,
        db: Session,
        object_type_id: str,
        *,
        name: str | None = None,
        display_name: str | None = None,
        description: str | None = None,
        operator: str | None = None,
    ) -> ObjectTypeDetail:
        from app.services.query import OntologyQueryService

        obj = db.get(ObjectType, object_type_id)
        if not obj:
            raise ValueError("Object type not found")

        if name is not None:
            obj.name = name
        if display_name is not None:
            obj.display_name = display_name
        if description is not None:
            obj.description = description

        if obj.status != EntityStatus.PRE_PUBLISHED.value:
            obj.status = EntityStatus.EDITED.value

        _log_change(db, "object_type", obj.id, "edit", operator, "更新对象类型")
        db.commit()

        detail = OntologyQueryService().get_object_type(db, object_type_id)
        if not detail:
            raise ValueError("Object type not found")
        return detail

    async def ensure_object_type_from_dataset(
        self,
        db: Session,
        ontology_id: str,
        dataset_urn: str,
        *,
        operator: str | None = None,
    ) -> ObjectTypeSummary:
        """根据 DataHub dataset urn 查找或创建对应 ObjectType（用于关系承载表）。

        - 已存在同 source_ref 的 ObjectType：直接返回
        - 不存在：从 DataHub（或 mock）拉取 dataset 元数据，创建 ObjectType
        """
        from app.connectors.datahub import DataHubConnector
        from app.services.evidence_builder import _infer_object_name
        from app.services.query import OntologyQueryService
        from app.services.settings_service import SettingsService

        ontology = db.get(Ontology, ontology_id)
        if not ontology:
            raise ValueError("Ontology not found")

        existing = (
            db.query(ObjectType)
            .filter(
                ObjectType.ontology_id == ontology_id,
                ObjectType.source_ref == dataset_urn,
            )
            .first()
        )
        if existing:
            return OntologyQueryService()._to_object_summary(db, existing)

        connector = DataHubConnector(SettingsService().get_datahub_runtime(db))
        dataset = await connector.get_dataset_by_urn(dataset_urn)

        object_name = _infer_object_name(dataset.name)
        candidate_name = object_name
        suffix = 1
        while (
            db.query(ObjectType)
            .filter(
                ObjectType.ontology_id == ontology_id,
                ObjectType.name == candidate_name,
            )
            .first()
        ):
            suffix += 1
            candidate_name = f"{object_name}_{suffix}"

        obj = ObjectType(
            ontology_id=ontology_id,
            name=candidate_name,
            display_name=dataset.display_name or dataset.name,
            description=dataset.description,
            source_ref=dataset.urn,
            source_confidence=0.5,
            status=EntityStatus.SUGGESTED.value,
        )
        db.add(obj)
        db.flush()
        _log_change(
            db,
            "object_type",
            obj.id,
            "create",
            operator,
            f"从 DataHub dataset 创建承载表对象：{dataset.name}",
        )
        db.commit()
        db.refresh(obj)
        return OntologyQueryService()._to_object_summary(db, obj)

    def update_property(
        self,
        db: Session,
        property_id: str,
        *,
        display_name: str | None = None,
        description: str | None = None,
        data_type: str | None = None,
        semantic_type: str | None = None,
        operator: str | None = None,
    ) -> PropertyOut:
        prop = db.get(Property, property_id)
        if not prop:
            raise ValueError("Property not found")

        if display_name is not None:
            prop.display_name = display_name
        if description is not None:
            prop.description = description
        if data_type is not None:
            prop.data_type = data_type
        if semantic_type is not None:
            prop.semantic_type = semantic_type

        if prop.status != EntityStatus.PRE_PUBLISHED.value:
            prop.status = EntityStatus.EDITED.value

        obj = db.get(ObjectType, prop.object_type_id)
        if obj and obj.status != EntityStatus.PRE_PUBLISHED.value:
            obj.status = EntityStatus.EDITED.value

        _log_change(db, "property", prop.id, "edit", operator, "更新属性")
        db.commit()
        db.refresh(prop)
        return PropertyOut.model_validate(prop)

    def create_relation_type(
        self,
        db: Session,
        ontology_id: str,
        *,
        display_name: str,
        source_object_type_id: str,
        target_object_type_id: str,
        name: str | None = None,
        description: str | None = None,
        cardinality: str | None = None,
        structure_type: str | None = None,
        mapping_object_type_id: str | None = None,
        operator: str | None = None,
    ) -> RelationTypeOut:
        from app.services.query import OntologyQueryService
        from app.services.relation_structure import validate_relation_structure_type
        from app.services.relation_terms import compact_relation_term, validate_relation_term

        ontology = db.get(Ontology, ontology_id)
        if not ontology:
            raise ValueError("Ontology not found")

        source = db.get(ObjectType, source_object_type_id)
        if not source or source.ontology_id != ontology_id:
            raise ValueError("Invalid source object type")
        target = db.get(ObjectType, target_object_type_id)
        if not target or target.ontology_id != ontology_id:
            raise ValueError("Invalid target object type")
        if source_object_type_id == target_object_type_id:
            raise ValueError("Source and target object cannot be the same")

        term_error = validate_relation_term(display_name)
        if term_error:
            raise ValueError(term_error)
        compacted = compact_relation_term(display_name)

        if structure_type is not None:
            structure_error = validate_relation_structure_type(structure_type)
            if structure_error:
                raise ValueError(structure_error)

        if mapping_object_type_id is not None:
            mapping_obj = db.get(ObjectType, mapping_object_type_id)
            if not mapping_obj or mapping_obj.ontology_id != ontology_id:
                raise ValueError("Invalid mapping object type")
            if mapping_object_type_id in {source_object_type_id, target_object_type_id}:
                raise ValueError("Mapping object cannot be the same as source or target")

        rel_name = name or compacted
        rel = RelationType(
            ontology_id=ontology_id,
            name=rel_name,
            display_name=compacted,
            description=description,
            source_object_type_id=source_object_type_id,
            target_object_type_id=target_object_type_id,
            cardinality=cardinality,
            structure_type=structure_type,
            mapping_object_type_id=mapping_object_type_id,
            source_confidence=0.5,
            status=EntityStatus.SUGGESTED.value,
        )
        db.add(rel)
        db.flush()
        _log_change(db, "relation_type", rel.id, "create", operator, f"新建关系：{compacted}")
        db.commit()
        return OntologyQueryService()._to_relation_out(db, rel)

    def update_relation_type(
        self,
        db: Session,
        relation_type_id: str,
        *,
        display_name: str | None = None,
        description: str | None = None,
        cardinality: str | None = None,
        structure_type: str | None = None,
        mapping_object_type_id: str | None = None,
        source_object_type_id: str | None = None,
        target_object_type_id: str | None = None,
        operator: str | None = None,
    ) -> RelationTypeOut:
        from app.services.query import OntologyQueryService
        from app.services.relation_structure import validate_relation_structure_type

        rel = db.get(RelationType, relation_type_id)
        if not rel:
            raise ValueError("Relation type not found")

        if display_name is not None:
            term_error = validate_relation_term(display_name)
            if term_error:
                raise ValueError(term_error)
            rel.display_name = compact_relation_term(display_name)
        if description is not None:
            rel.description = description
        if cardinality is not None:
            rel.cardinality = cardinality
        if structure_type is not None:
            structure_error = validate_relation_structure_type(structure_type)
            if structure_error:
                raise ValueError(structure_error)
            rel.structure_type = structure_type
        if mapping_object_type_id is not None:
            if mapping_object_type_id == "":
                rel.mapping_object_type_id = None
            else:
                mapping_obj = db.get(ObjectType, mapping_object_type_id)
                if not mapping_obj or mapping_obj.ontology_id != rel.ontology_id:
                    raise ValueError("Invalid mapping object type")
                if mapping_object_type_id in {
                    rel.source_object_type_id,
                    rel.target_object_type_id,
                }:
                    raise ValueError("Mapping object cannot be the same as source or target")
                rel.mapping_object_type_id = mapping_object_type_id
        if source_object_type_id is not None:
            source = db.get(ObjectType, source_object_type_id)
            if not source or source.ontology_id != rel.ontology_id:
                raise ValueError("Invalid source object type")
            if source_object_type_id == rel.target_object_type_id:
                raise ValueError("Source and target object cannot be the same")
            rel.source_object_type_id = source_object_type_id
        if target_object_type_id is not None:
            target = db.get(ObjectType, target_object_type_id)
            if not target or target.ontology_id != rel.ontology_id:
                raise ValueError("Invalid target object type")
            if target_object_type_id == rel.source_object_type_id:
                raise ValueError("Source and target object cannot be the same")
            rel.target_object_type_id = target_object_type_id

        if rel.status != EntityStatus.PRE_PUBLISHED.value:
            rel.status = EntityStatus.EDITED.value

        _log_change(db, "relation_type", rel.id, "edit", operator, "更新关系")
        db.commit()
        return OntologyQueryService()._to_relation_out(db, rel)

    def pre_publish_relation_type(
        self,
        db: Session,
        relation_type_id: str,
        operator: str | None = None,
    ) -> RelationTypeOut:
        from app.services.query import OntologyQueryService

        rel = db.get(RelationType, relation_type_id)
        if not rel:
            raise ValueError("Relation type not found")

        rel.status = EntityStatus.PRE_PUBLISHED.value
        _log_change(db, "relation_type", rel.id, "pre_publish", operator, "预发布关系")
        db.commit()
        db.refresh(rel)
        return OntologyQueryService()._to_relation_out(db, rel)

    def pre_publish_object_type(
        self,
        db: Session,
        object_type_id: str,
        operator: str | None = None,
    ) -> ObjectTypeSummary:
        from app.services.query import OntologyQueryService

        obj = db.get(ObjectType, object_type_id)
        if not obj:
            raise ValueError("Object type not found")

        obj.status = EntityStatus.PRE_PUBLISHED.value
        _log_change(db, "object_type", obj.id, "pre_publish", operator, "预发布")
        db.commit()
        db.refresh(obj)
        return OntologyQueryService()._to_object_summary(db, obj)

    # --- 业务逻辑本体编辑(定义 / 预发布)---
    # 注:对象/字段引用绑定复用下方 bind_object_to_logic / bind_property_to_logic,
    # 由用户在业务逻辑详情页从已发布本体中主动挑选。

    def _resolve_published_ontology(self, db: Session, domain_id: str) -> Ontology:
        from app.services.query import OntologyQueryService

        domain = db.get(DomainContext, domain_id)
        if not domain:
            raise ValueError("数据域不存在")
        ontology = OntologyQueryService().get_published_ontology(db, domain_id)
        if not ontology:
            raise ValueError("该数据域尚无已发布本体,无法创建业务逻辑")
        return ontology

    def _ensure_unique_logic_name(self, db: Session, ontology_id: str, name: str) -> str:
        base = name or "business_logic"
        candidate = base
        suffix = 1
        while (
            db.query(BusinessLogic)
            .filter(
                BusinessLogic.ontology_id == ontology_id,
                BusinessLogic.name == candidate,
            )
            .first()
        ):
            suffix += 1
            candidate = f"{base}_{suffix}"
        return candidate

    def create_business_logic(
        self,
        db: Session,
        *,
        domain_id: str,
        name: str,
        display_name: str,
        logic_type: str,
        description: str | None = None,
        expression_summary: str | None = None,
        operator: str | None = None,
    ) -> BusinessLogicDetail:
        from app.services.query import OntologyQueryService

        ontology = self._resolve_published_ontology(db, domain_id)
        if logic_type not in {"metric", "tag", "rule"}:
            raise ValueError("logic_type 必须是 metric / tag / rule 之一")
        unique_name = self._ensure_unique_logic_name(db, ontology.id, name)

        logic = BusinessLogic(
            ontology_id=ontology.id,
            name=unique_name,
            display_name=display_name,
            logic_type=logic_type,
            description=description,
            expression_summary=expression_summary,
            source_type="manual",
            source_ref=None,
            source_confidence=0.5,
            status=EntityStatus.SUGGESTED.value,
        )
        db.add(logic)
        db.flush()
        _log_change(db, "business_logic", logic.id, "create", operator, f"新建业务逻辑:{display_name}")
        db.commit()

        detail = OntologyQueryService().get_business_logic(db, logic.id)
        if not detail:
            raise ValueError("Business logic not found")
        return detail

    def update_business_logic(
        self,
        db: Session,
        logic_id: str,
        *,
        display_name: str | None = None,
        description: str | None = None,
        logic_type: str | None = None,
        expression_summary: str | None = None,
        operator: str | None = None,
    ) -> BusinessLogicDetail:
        from app.services.query import OntologyQueryService

        logic = db.get(BusinessLogic, logic_id)
        if not logic:
            raise ValueError("Business logic not found")

        if logic_type is not None:
            if logic_type not in {"metric", "tag", "rule"}:
                raise ValueError("logic_type 必须是 metric / tag / rule 之一")
            logic.logic_type = logic_type
        if display_name is not None:
            logic.display_name = display_name
        if description is not None:
            logic.description = description
        if expression_summary is not None:
            logic.expression_summary = expression_summary

        if logic.status != EntityStatus.PRE_PUBLISHED.value:
            logic.status = EntityStatus.EDITED.value

        _log_change(db, "business_logic", logic.id, "edit", operator, "更新业务逻辑")
        db.commit()

        detail = OntologyQueryService().get_business_logic(db, logic_id)
        if not detail:
            raise ValueError("Business logic not found")
        return detail

    def pre_publish_business_logic(
        self,
        db: Session,
        logic_id: str,
        operator: str | None = None,
    ) -> BusinessLogicOut:
        logic = db.get(BusinessLogic, logic_id)
        if not logic:
            raise ValueError("Business logic not found")

        logic.status = EntityStatus.PRE_PUBLISHED.value
        _log_change(db, "business_logic", logic.id, "pre_publish", operator, "预发布业务逻辑")
        db.commit()
        db.refresh(logic)

        from app.services.query import OntologyQueryService

        return OntologyQueryService()._to_business_logic_out(db, logic)

    # --- 业务逻辑绑定（对象 / 字段）---

    def _logic_or_raise(self, db: Session, logic_id: str) -> BusinessLogic:
        logic = db.get(BusinessLogic, logic_id)
        if not logic:
            raise ValueError("Business logic not found")
        return logic

    def _object_same_ontology(
        self, db: Session, logic: BusinessLogic, object_type_id: str
    ) -> ObjectType:
        obj = db.get(ObjectType, object_type_id)
        if not obj or obj.ontology_id != logic.ontology_id:
            raise ValueError("Object type not found or not in the same ontology")
        return obj

    def _property_same_ontology(
        self, db: Session, logic: BusinessLogic, property_id: str
    ) -> Property:
        prop = db.get(Property, property_id)
        if not prop:
            raise ValueError("Property not found")
        obj = db.get(ObjectType, prop.object_type_id)
        if not obj or obj.ontology_id != logic.ontology_id:
            raise ValueError("Property not found or not in the same ontology")
        return prop

    def bind_object_to_logic(
        self,
        db: Session,
        logic_id: str,
        object_type_id: str,
        *,
        role: str = "subject",
        operator: str | None = None,
    ) -> BusinessLogicObjectBindingOut:
        if role not in _OBJECT_BINDING_ROLES:
            raise ValueError(f"Invalid role, allowed: {sorted(_OBJECT_BINDING_ROLES)}")
        logic = self._logic_or_raise(db, logic_id)
        obj = self._object_same_ontology(db, logic, object_type_id)

        existing = (
            db.query(BusinessLogicObjectBinding)
            .filter(
                BusinessLogicObjectBinding.business_logic_id == logic.id,
                BusinessLogicObjectBinding.object_type_id == obj.id,
                BusinessLogicObjectBinding.role == role,
            )
            .first()
        )
        if existing:
            if existing.source == "inferred":
                existing.source = "manual"
                db.commit()
                db.refresh(existing)
            return BusinessLogicObjectBindingOut.model_validate(
                self._enrich_object_binding(db, existing)
            )

        binding = BusinessLogicObjectBinding(
            business_logic_id=logic.id,
            object_type_id=obj.id,
            role=role,
            source="manual",
        )
        db.add(binding)
        db.flush()
        _log_change(
            db,
            "business_logic",
            logic.id,
            "bind_object",
            operator,
            f"绑定对象 {obj.display_name}（role={role}）",
        )
        db.commit()
        db.refresh(binding)
        return BusinessLogicObjectBindingOut.model_validate(
            self._enrich_object_binding(db, binding)
        )

    def unbind_object_from_logic(
        self,
        db: Session,
        binding_id: str,
        *,
        operator: str | None = None,
    ) -> dict:
        binding = db.get(BusinessLogicObjectBinding, binding_id)
        if not binding:
            raise ValueError("Object binding not found")
        logic_id = binding.business_logic_id
        object_type_id = binding.object_type_id
        role = binding.role
        db.delete(binding)
        _log_change(
            db,
            "business_logic",
            logic_id,
            "unbind_object",
            operator,
            f"解绑对象 {object_type_id}（role={role}）",
        )
        db.commit()
        return {"id": binding_id, "deleted": True}

    def bind_property_to_logic(
        self,
        db: Session,
        logic_id: str,
        property_id: str,
        *,
        role: str = "input",
        operator: str | None = None,
    ) -> BusinessLogicPropertyBindingOut:
        if role not in _PROPERTY_BINDING_ROLES:
            raise ValueError(f"Invalid role, allowed: {sorted(_PROPERTY_BINDING_ROLES)}")
        logic = self._logic_or_raise(db, logic_id)
        prop = self._property_same_ontology(db, logic, property_id)

        existing = (
            db.query(BusinessLogicPropertyBinding)
            .filter(
                BusinessLogicPropertyBinding.business_logic_id == logic.id,
                BusinessLogicPropertyBinding.property_id == prop.id,
                BusinessLogicPropertyBinding.role == role,
            )
            .first()
        )
        if existing:
            if existing.source == "inferred":
                existing.source = "manual"
                db.commit()
                db.refresh(existing)
            return BusinessLogicPropertyBindingOut.model_validate(
                self._enrich_property_binding(db, existing)
            )

        binding = BusinessLogicPropertyBinding(
            business_logic_id=logic.id,
            property_id=prop.id,
            role=role,
            source="manual",
        )
        db.add(binding)
        db.flush()
        _log_change(
            db,
            "business_logic",
            logic.id,
            "bind_property",
            operator,
            f"绑定字段 {prop.display_name}（role={role}）",
        )
        db.commit()
        db.refresh(binding)
        return BusinessLogicPropertyBindingOut.model_validate(
            self._enrich_property_binding(db, binding)
        )

    def unbind_property_from_logic(
        self,
        db: Session,
        binding_id: str,
        *,
        operator: str | None = None,
    ) -> dict:
        binding = db.get(BusinessLogicPropertyBinding, binding_id)
        if not binding:
            raise ValueError("Property binding not found")
        logic_id = binding.business_logic_id
        property_id = binding.property_id
        role = binding.role
        db.delete(binding)
        _log_change(
            db,
            "business_logic",
            logic_id,
            "unbind_property",
            operator,
            f"解绑字段 {property_id}（role={role}）",
        )
        db.commit()
        return {"id": binding_id, "deleted": True}

    @staticmethod
    def _enrich_object_binding(
        db: Session, binding: BusinessLogicObjectBinding
    ) -> BusinessLogicObjectBinding:
        obj = db.get(ObjectType, binding.object_type_id)
        # 通过 setattr 附加显示字段；Pydantic from_attributes 会读取这些属性
        if obj:
            setattr(binding, "object_type_name", obj.name)
            setattr(binding, "object_type_display_name", obj.display_name)
        return binding

    @staticmethod
    def _enrich_property_binding(
        db: Session, binding: BusinessLogicPropertyBinding
    ) -> BusinessLogicPropertyBinding:
        prop = db.get(Property, binding.property_id)
        if prop:
            setattr(binding, "property_name", prop.name)
            setattr(binding, "property_display_name", prop.display_name)
            obj = db.get(ObjectType, prop.object_type_id)
            if obj:
                setattr(binding, "object_type_id", obj.id)
                setattr(binding, "object_type_name", obj.name)
        return binding
