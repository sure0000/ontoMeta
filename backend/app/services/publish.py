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
    EntityChangeLog,
    EntityStatus,
    ObjectType,
    Ontology,
    OntologyStatus,
    Property,
    RelationType,
    VersionRecord,
)
from app.schemas import ConfirmationCreate, OntologyDraftOutput


def _log_change(
    db: Session,
    entity_type: str,
    entity_id: str,
    action: str,
    operator: str | None = None,
    summary: str | None = None,
) -> None:
    db.add(
        EntityChangeLog(
            entity_type=entity_type,
            entity_id=entity_id,
            action=action,
            operator=operator,
            change_summary=summary,
        )
    )


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

        for item in draft.object_types:
            obj = ObjectType(
                ontology_id=ontology.id,
                name=item.name,
                display_name=item.display_name,
                description=item.description,
                source_ref=item.source_ref,
                source_confidence=item.confidence,
                status=EntityStatus.SUGGESTED.value,
            )
            db.add(obj)
            db.flush()
            object_name_to_id[item.name] = obj.id
            object_field_to_property_id[item.name] = {}

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
            db.flush()
            object_field_to_property_id.setdefault(item.object_type_name, {})[
                item.name
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
            db.flush()
            logic_name_to_id[item.name] = logic.id

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


class PublishService:
    """将编辑确认后的草稿发布为正式版本。"""

    def publish(self, db: Session, ontology_id: str, operator: str | None = None) -> Ontology:
        ontology = db.get(Ontology, ontology_id)
        if not ontology:
            raise ValueError("Ontology not found")

        ontology.version += 1
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
                diff_summary=f"发布本体版本 v{ontology.version}",
                operator=operator,
            )
        )
        _log_change(db, "ontology", ontology.id, "publish", operator, f"v{ontology.version}")
        db.commit()
        db.refresh(ontology)
        return ontology


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
            self.publish_service.publish(db, confirmation.ontology_id, confirmation.operator)
        elif confirmation.action_type == "delete" and confirmation.target_id:
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
