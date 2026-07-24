import json
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import (
    BusinessLogic,
    BusinessLogicObjectBinding,
    BusinessLogicPropertyBinding,
    ChangeConfirmation,
    ConfirmationStatus,
    DraftEvidence,
    EntityStatus,
    ObjectType,
    Ontology,
    OntologyStatus,
    Property,
    RelationType,
    VersionRecord,
)
from app.schemas import (
    ConfirmationCreate,
    DraftObjectType,
    DraftProperty,
    DraftRelationType,
    OntologyDraftOutput,
)
from app.services.common import log_change
from app.services.draft_consistency import DraftConsistencyError, assert_ontology_consistent
from app.services.version_diff import (
    capture_ontology_snapshot,
    compute_version_diff,
    load_previous_snapshot,
    summarize_diff,
)


def _log_change(
    db: Session,
    entity_type: str,
    entity_id: str,
    action: str,
    operator: str | None = None,
    summary: str | None = None,
) -> None:
    log_change(db, entity_type, entity_id, action, operator, summary)


class DraftPersistenceService:
    """持久化草稿与证据引用。"""

    def save_draft(
        self,
        db: Session,
        ontology: Ontology,
        draft: OntologyDraftOutput,
    ) -> Ontology:
        object_name_to_id: dict[str, str] = {}
        # object_type_name -> { field_name -> property_id }
        object_field_to_property_id: dict[str, dict[str, str]] = {}

        object_models: list[tuple[str, ObjectType]] = []
        for item in draft.object_types:
            obj = ObjectType(
                ontology_id=ontology.id,
                name=item.name,
                display_name=item.display_name,
                description=item.description,
                source_ref=item.source_ref,
                source_confidence=item.confidence,
                table_role=item.table_role,
                role_confidence=item.role_confidence,
                role_reason=item.role_reason,
                status=EntityStatus.SUGGESTED.value,
            )
            db.add(obj)
            object_models.append((item.name, obj))
        db.flush()
        for name, obj in object_models:
            object_name_to_id[name] = obj.id
            object_field_to_property_id[name] = {}

        property_models: list[tuple[str, str, Property]] = []
        for item in draft.properties:
            object_type_id = object_name_to_id.get(item.object_type_name)
            if not object_type_id:
                continue
            prop = Property(
                object_type_id=object_type_id,
                name=item.name,
                display_name=item.display_name,
                description=item.description,
                data_type=item.data_type,
                semantic_type=item.semantic_type,
                source_field_ref=item.source_field_ref,
                required=item.required,
                source_confidence=item.confidence,
                status=EntityStatus.SUGGESTED.value,
            )
            db.add(prop)
            property_models.append((item.object_type_name, item.name, prop))
        if property_models:
            db.flush()
            for object_type_name, field_name, prop in property_models:
                object_field_to_property_id.setdefault(object_type_name, {})[
                    field_name
                ] = prop.id

        for item in draft.relation_types:
            source_id = object_name_to_id.get(item.source_object_type_name)
            target_id = object_name_to_id.get(item.target_object_type_name)
            if not source_id or not target_id:
                continue
            db.add(
                RelationType(
                    ontology_id=ontology.id,
                    name=item.name,
                    display_name=item.display_name,
                    description=item.description,
                    source_object_type_id=source_id,
                    target_object_type_id=target_id,
                    cardinality=item.cardinality,
                    structure_type=item.structure_type,
                    source_evidence=item.source_evidence,
                    source_confidence=item.confidence,
                    status=EntityStatus.SUGGESTED.value,
                )
            )

        logic_name_to_id: dict[str, str] = {}
        logic_models: list[tuple[str, BusinessLogic]] = []
        for item in draft.business_logics:
            logic = BusinessLogic(
                ontology_id=ontology.id,
                name=item.name,
                display_name=item.display_name,
                logic_type=item.logic_type,
                description=item.description,
                expression_summary=item.expression_summary,
                source_type=item.source_type,
                source_ref=item.source_ref,
                source_confidence=item.confidence,
                status=EntityStatus.SUGGESTED.value,
            )
            db.add(logic)
            logic_models.append((item.name, logic))
        if logic_models:
            db.flush()
            for name, logic in logic_models:
                logic_name_to_id[name] = logic.id

        for item in draft.business_logic_object_bindings:
            logic_id = logic_name_to_id.get(item.logic_name)
            object_type_id = object_name_to_id.get(item.object_type_name)
            if not logic_id or not object_type_id:
                continue
            db.add(
                BusinessLogicObjectBinding(
                    business_logic_id=logic_id,
                    object_type_id=object_type_id,
                    role=item.role,
                    source="inferred",
                    confidence=item.confidence,
                )
            )

        for item in draft.business_logic_property_bindings:
            logic_id = logic_name_to_id.get(item.logic_name)
            property_id = object_field_to_property_id.get(
                item.object_type_name, {}
            ).get(item.field_name)
            if not logic_id or not property_id:
                continue
            db.add(
                BusinessLogicPropertyBinding(
                    business_logic_id=logic_id,
                    property_id=property_id,
                    role=item.role,
                    source="inferred",
                    confidence=item.confidence,
                )
            )

        for ref in draft.evidence_refs:
            db.add(
                DraftEvidence(
                    ontology_id=ontology.id,
                    evidence_type="datahub_ref",
                    source_ref=ref,
                    payload_summary=ref,
                    confidence=0.5,
                )
            )

        ontology.generated_at = datetime.now(timezone.utc)
        ontology.status = OntologyStatus.DRAFT.value
        db.commit()
        db.refresh(ontology)
        return ontology

    def upsert_objects(
        self,
        db: Session,
        ontology: Ontology,
        object_types: list[DraftObjectType],
        properties: list[DraftProperty],
    ) -> dict[str, str]:
        """按 source_ref(数据集 urn) upsert 对象与属性到已有草稿本体。

        不删除本体下已有的关系，也不删除评估中已消失的对象/属性——用于
        「仅生成业务对象」独立执行，可与「仅生成业务关系」并行，互不清空
        对方产出。返回 source_ref -> object_type_id，供关系生成按 urn 精确回链。
        """
        existing_by_ref: dict[str, ObjectType] = {
            obj.source_ref: obj
            for obj in db.query(ObjectType)
            .filter(ObjectType.ontology_id == ontology.id)
            .all()
            if obj.source_ref
        }

        object_ref_to_id: dict[str, str] = {}
        object_id_by_name: dict[str, str] = {}
        for item in object_types:
            existing = existing_by_ref.get(item.source_ref) if item.source_ref else None
            if existing is not None:
                existing.name = item.name
                existing.display_name = item.display_name
                existing.description = item.description
                existing.source_confidence = item.confidence
                existing.table_role = item.table_role
                existing.role_confidence = item.role_confidence
                existing.role_reason = item.role_reason
                obj = existing
            else:
                obj = ObjectType(
                    ontology_id=ontology.id,
                    name=item.name,
                    display_name=item.display_name,
                    description=item.description,
                    source_ref=item.source_ref,
                    source_confidence=item.confidence,
                    table_role=item.table_role,
                    role_confidence=item.role_confidence,
                    role_reason=item.role_reason,
                    status=EntityStatus.SUGGESTED.value,
                )
                db.add(obj)
            db.flush()
            if item.source_ref:
                object_ref_to_id[item.source_ref] = obj.id
            object_id_by_name[item.name] = obj.id

        existing_props_by_object: dict[str, dict[str, Property]] = {}
        if object_id_by_name:
            for prop in (
                db.query(Property)
                .filter(Property.object_type_id.in_(list(object_id_by_name.values())))
                .all()
            ):
                existing_props_by_object.setdefault(prop.object_type_id, {})[
                    prop.source_field_ref or prop.name
                ] = prop

        for item in properties:
            object_type_id = object_id_by_name.get(item.object_type_name)
            if not object_type_id:
                continue
            key = item.source_field_ref or item.name
            existing_prop = existing_props_by_object.get(object_type_id, {}).get(key)
            if existing_prop is not None:
                existing_prop.name = item.name
                existing_prop.display_name = item.display_name
                existing_prop.description = item.description
                existing_prop.data_type = item.data_type
                existing_prop.semantic_type = item.semantic_type
                existing_prop.source_field_ref = item.source_field_ref
                existing_prop.required = item.required
                existing_prop.source_confidence = item.confidence
            else:
                db.add(
                    Property(
                        object_type_id=object_type_id,
                        name=item.name,
                        display_name=item.display_name,
                        description=item.description,
                        data_type=item.data_type,
                        semantic_type=item.semantic_type,
                        source_field_ref=item.source_field_ref,
                        required=item.required,
                        source_confidence=item.confidence,
                        status=EntityStatus.SUGGESTED.value,
                    )
                )

        ontology.generated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(ontology)
        return object_ref_to_id

    def upsert_relations(
        self,
        db: Session,
        ontology: Ontology,
        relation_types: list[DraftRelationType],
        object_id_by_candidate: dict[str, str],
    ) -> int:
        """按 name upsert 关系类型到已有草稿本体，不触碰该本体的对象/属性。

        ``relation_types`` 的 source/target 对象名是证据 candidate_name(未经
        业务命名提升，见 ``OntologyDraftGenerator.generate_relations``)；
        ``object_id_by_candidate`` 由调用方按 source_dataset_urn 把 candidate_name
        回链到已入库的 ObjectType.id。两端有一端回链不到(如对象尚未生成)的
        关系会被跳过，不计入返回的已写入数量。
        """
        existing_by_name = {
            rel.name: rel
            for rel in db.query(RelationType)
            .filter(RelationType.ontology_id == ontology.id)
            .all()
        }

        written = 0
        for item in relation_types:
            source_id = object_id_by_candidate.get(item.source_object_type_name)
            target_id = object_id_by_candidate.get(item.target_object_type_name)
            if not source_id or not target_id:
                continue
            existing = existing_by_name.get(item.name)
            if existing is not None:
                existing.display_name = item.display_name
                existing.description = item.description
                existing.source_object_type_id = source_id
                existing.target_object_type_id = target_id
                existing.cardinality = item.cardinality
                existing.structure_type = item.structure_type
                existing.source_evidence = item.source_evidence
                existing.source_confidence = item.confidence
            else:
                db.add(
                    RelationType(
                        ontology_id=ontology.id,
                        name=item.name,
                        display_name=item.display_name,
                        description=item.description,
                        source_object_type_id=source_id,
                        target_object_type_id=target_id,
                        cardinality=item.cardinality,
                        structure_type=item.structure_type,
                        source_evidence=item.source_evidence,
                        source_confidence=item.confidence,
                        status=EntityStatus.SUGGESTED.value,
                    )
                )
            written += 1

        ontology.generated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(ontology)
        return written


class PublishService:
    """将编辑确认后的草稿发布为正式版本。"""

    def publish(self, db: Session, ontology_id: str, operator: str | None = None) -> Ontology:
        ontology = db.get(Ontology, ontology_id)
        if not ontology:
            raise ValueError("Ontology not found")

        assert_ontology_consistent(db, ontology_id)

        new_version = ontology.version + 1
        previous_snapshot = load_previous_snapshot(
            db, ontology_id, before_version=new_version
        )
        current_snapshot = capture_ontology_snapshot(db, ontology_id)
        diff = compute_version_diff(previous_snapshot, current_snapshot)
        diff_summary = f"发布本体版本 v{new_version}：{summarize_diff(diff)}"

        ontology.version = new_version
        ontology.status = OntologyStatus.PUBLISHED.value
        ontology.published_at = datetime.now(timezone.utc)
        ontology.approved_by = operator

        entities: list = []
        entities.extend(db.query(ObjectType).filter(ObjectType.ontology_id == ontology_id).all())
        entities.extend(
            db.query(Property).join(ObjectType).filter(ObjectType.ontology_id == ontology_id).all()
        )
        entities.extend(db.query(RelationType).filter(RelationType.ontology_id == ontology_id).all())
        entities.extend(
            db.query(BusinessLogic).filter(BusinessLogic.ontology_id == ontology_id).all()
        )

        for entity in entities:
            if entity.status != EntityStatus.DEPRECATED.value:
                entity.status = EntityStatus.PUBLISHED.value

        db.add(
            VersionRecord(
                entity_type="ontology",
                entity_id=ontology.id,
                version=ontology.version,
                diff_summary=diff_summary,
                diff_json=json.dumps(diff, ensure_ascii=False),
                snapshot_json=json.dumps(current_snapshot, ensure_ascii=False),
                operator=operator,
            )
        )
        _log_change(db, "ontology", ontology.id, "publish", operator, f"v{ontology.version}")
        db.commit()
        db.refresh(ontology)
        return ontology

    def publish_business_logic(
        self, db: Session, logic_id: str, operator: str | None = None
    ) -> BusinessLogic:
        """发布单条业务逻辑:置为 published,引用绑定即固化为与已发布本体的正式绑定。"""
        logic = db.get(BusinessLogic, logic_id)
        if not logic:
            raise ValueError("Business logic not found")

        logic.status = EntityStatus.PUBLISHED.value
        # 版本号沿用其所属本体当前版本,作为该逻辑的发布快照记录
        ontology = db.get(Ontology, logic.ontology_id)
        version = ontology.version if ontology else 0
        db.add(
            VersionRecord(
                entity_type="business_logic",
                entity_id=logic.id,
                version=version,
                diff_summary=f"发布业务逻辑:{logic.display_name}",
                operator=operator,
            )
        )
        _log_change(db, "business_logic", logic.id, "publish", operator, "发布业务逻辑")
        db.commit()
        db.refresh(logic)
        return logic


class ConfirmationService:
    """重要操作二次确认。"""

    def __init__(self) -> None:
        self.publish_service = PublishService()

    def create(self, db: Session, data: ConfirmationCreate) -> ChangeConfirmation:
        confirmation = ChangeConfirmation(
            ontology_id=data.ontology_id,
            target_type=data.target_type,
            target_id=data.target_id,
            action_type=data.action_type,
            operator=data.operator,
            reason=data.reason,
            payload=json.dumps(data.payload, ensure_ascii=False) if data.payload else None,
            confirmation_status=ConfirmationStatus.PENDING.value,
        )
        db.add(confirmation)
        db.commit()
        db.refresh(confirmation)
        return confirmation

    def get(self, db: Session, confirmation_id: str) -> ChangeConfirmation | None:
        return db.get(ChangeConfirmation, confirmation_id)

    def confirm(
        self, db: Session, confirmation_id: str, operator: str | None = None
    ) -> ChangeConfirmation:
        confirmation = db.get(ChangeConfirmation, confirmation_id)
        if not confirmation:
            raise ValueError("Confirmation not found")
        if confirmation.confirmation_status != ConfirmationStatus.PENDING.value:
            raise ValueError("Confirmation is not pending")

        confirmation.confirmation_status = ConfirmationStatus.CONFIRMED.value
        confirmation.confirmed_at = datetime.now(timezone.utc)
        if operator:
            confirmation.operator = operator

        if confirmation.action_type == "publish":
            if confirmation.target_type == "business_logic" and confirmation.target_id:
                self.publish_service.publish_business_logic(
                    db, confirmation.target_id, confirmation.operator
                )
            else:
                self.publish_service.publish(db, confirmation.ontology_id, confirmation.operator)
        elif confirmation.action_type == "delete" and confirmation.target_id:
            if confirmation.target_type == "business_logic":
                logic = db.get(BusinessLogic, confirmation.target_id)
                if logic:
                    db.delete(logic)
            _log_change(
                db,
                confirmation.target_type,
                confirmation.target_id,
                "delete",
                confirmation.operator,
            )

        db.commit()
        db.refresh(confirmation)
        return confirmation

    def cancel(self, db: Session, confirmation_id: str) -> ChangeConfirmation:
        confirmation = db.get(ChangeConfirmation, confirmation_id)
        if not confirmation:
            raise ValueError("Confirmation not found")
        if confirmation.confirmation_status != ConfirmationStatus.PENDING.value:
            raise ValueError("Confirmation is not pending")
        confirmation.confirmation_status = ConfirmationStatus.CANCELLED.value
        db.commit()
        db.refresh(confirmation)
        return confirmation
