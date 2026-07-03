from datetime import datetime, timezone

from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from app.models import (
    BusinessLogic,
    BusinessLogicObjectBinding,
    BusinessLogicPropertyBinding,
    ChangeConfirmation,
    DomainContext,
    DraftEvidence,
    DraftGenerationTask,
    EntityChangeLog,
    ObjectType,
    Ontology,
    OntologyStatus,
    Property,
    RelationType,
    VersionRecord,
)
from app.schemas import (
    BusinessLogicDetail,
    BusinessLogicObjectBindingOut,
    BusinessLogicOut,
    BusinessLogicPropertyBindingOut,
    ChangeLogOut,
    DomainContextDetail,
    DomainContextSummary,
    DraftProgressOut,
    GraphEdge,
    GraphNode,
    ObjectTypeDetail,
    ObjectTypeSummary,
    OntologyGraph,
    OntologySummary,
    PropertyOut,
    RelationObjectRef,
    RelationTypeDetail,
    RelationTypeOut,
    TaskRecordOut,
    VersionRecordOut,
)
from app.services.relation_structure import infer_relation_structure_type


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


def _logic_text_blob(logic: BusinessLogic) -> str:
    parts = [
        logic.name,
        logic.display_name,
        logic.description or "",
        logic.expression_summary or "",
        logic.source_ref or "",
    ]
    return " ".join(parts).lower()


def _logic_relates_to_object(logic: BusinessLogic, obj: ObjectType) -> bool:
    """文本兜底：仅在没有显式绑定的历史数据上使用。"""
    blob = _logic_text_blob(logic)
    tokens = {obj.name.lower(), obj.display_name.lower()}
    return any(token and token in blob for token in tokens)


def _object_relates_to_logic(obj: ObjectType, logic: BusinessLogic) -> bool:
    return _logic_relates_to_object(logic, obj)


def _normalize_cardinality(cardinality: str | None) -> str | None:
    if not cardinality:
        return None
    mapping = {
        "many_to_one": "N:1",
        "one_to_many": "1:N",
        "one_to_one": "1:1",
        "many_to_many": "N:M",
    }
    return mapping.get(cardinality, cardinality)


class OntologyQueryService:
    """只读查询服务。"""

    def _published_ontology_query(self, db: Session, domain_context_id: str | None = None):
        query = db.query(Ontology).filter(Ontology.status == OntologyStatus.PUBLISHED.value)
        if domain_context_id:
            query = query.filter(Ontology.domain_context_id == domain_context_id)
        return query

    def _published_ontology_ids(
        self, db: Session, domain_context_id: str | None = None
    ) -> list[str]:
        return [o.id for o in self._published_ontology_query(db, domain_context_id).all()]

    def get_published_ontology(
        self, db: Session, domain_context_id: str
    ) -> Ontology | None:
        return (
            self._published_ontology_query(db, domain_context_id)
            .order_by(Ontology.published_at.desc(), Ontology.version.desc())
            .first()
        )

    def _resolve_domain_context(
        self, db: Session, ontology_id: str
    ) -> tuple[str | None, str | None]:
        ontology = db.get(Ontology, ontology_id)
        if not ontology:
            return None, None
        domain = db.get(DomainContext, ontology.domain_context_id)
        if not domain:
            return ontology.domain_context_id, None
        return domain.id, domain.name

    def _apply_ontology_scope(
        self,
        db: Session,
        query,
        *,
        ontology_id: str | None = None,
        domain_context_id: str | None = None,
        published_only: bool = False,
        ontology_model=ObjectType,
    ):
        if ontology_id:
            if published_only:
                ontology = db.get(Ontology, ontology_id)
                if not ontology or ontology.status != OntologyStatus.PUBLISHED.value:
                    return query.filter(False)
            return query.filter(ontology_model.ontology_id == ontology_id)

        if domain_context_id:
            ontologies = db.query(Ontology).filter(
                Ontology.domain_context_id == domain_context_id
            )
            if published_only:
                ontologies = ontologies.filter(Ontology.status == OntologyStatus.PUBLISHED.value)
            ontology_ids = [o.id for o in ontologies.all()]
            if not ontology_ids:
                return query.filter(False)
            return query.filter(ontology_model.ontology_id.in_(ontology_ids))

        if published_only:
            ontology_ids = self._published_ontology_ids(db)
            if not ontology_ids:
                return query.filter(False)
            return query.filter(ontology_model.ontology_id.in_(ontology_ids))

        return query

    def _to_business_logic_out(
        self, db: Session, logic: BusinessLogic
    ) -> BusinessLogicOut:
        domain_id, domain_name = self._resolve_domain_context(db, logic.ontology_id)
        bound_object_count = (
            db.query(BusinessLogicObjectBinding)
            .filter(BusinessLogicObjectBinding.business_logic_id == logic.id)
            .distinct(BusinessLogicObjectBinding.object_type_id)
            .count()
        )
        bound_property_count = (
            db.query(BusinessLogicPropertyBinding)
            .filter(BusinessLogicPropertyBinding.property_id.isnot(None))
            .filter(BusinessLogicPropertyBinding.business_logic_id == logic.id)
            .distinct(BusinessLogicPropertyBinding.property_id)
            .count()
        )
        return BusinessLogicOut(
            id=logic.id,
            name=logic.name,
            display_name=logic.display_name,
            logic_type=logic.logic_type,
            description=logic.description,
            expression_summary=logic.expression_summary,
            source_type=logic.source_type,
            source_ref=logic.source_ref,
            status=logic.status,
            source_confidence=logic.source_confidence,
            domain_context_id=domain_id,
            domain_name=domain_name,
            bound_object_count=bound_object_count,
            bound_property_count=bound_property_count,
            updated_at=logic.updated_at,
        )

    def _logic_object_binding_ids(
        self, db: Session, logic: BusinessLogic
    ) -> list[str]:
        rows = (
            db.query(BusinessLogicObjectBinding.object_type_id)
            .filter(BusinessLogicObjectBinding.business_logic_id == logic.id)
            .distinct()
            .all()
        )
        return [r[0] for r in rows]

    def _object_logic_ids(self, db: Session, obj: ObjectType) -> list[str]:
        rows = (
            db.query(BusinessLogicObjectBinding.business_logic_id)
            .filter(BusinessLogicObjectBinding.object_type_id == obj.id)
            .distinct()
            .all()
        )
        return [r[0] for r in rows]

    def _related_logics_for_object(
        self, db: Session, obj: ObjectType
    ) -> list[BusinessLogic]:
        logic_ids = self._object_logic_ids(db, obj)
        if logic_ids:
            return (
                db.query(BusinessLogic)
                .filter(BusinessLogic.id.in_(logic_ids))
                .order_by(BusinessLogic.updated_at.desc())
                .all()
            )

        # 历史数据兜底：无显式绑定时回落到文本命中
        logics = (
            db.query(BusinessLogic)
            .filter(BusinessLogic.ontology_id == obj.ontology_id)
            .all()
        )
        related = [logic for logic in logics if _logic_relates_to_object(logic, obj)]
        if related:
            return related

        relation_peer_ids: set[str] = set()
        relations = (
            db.query(RelationType)
            .filter(
                (RelationType.source_object_type_id == obj.id)
                | (RelationType.target_object_type_id == obj.id)
            )
            .all()
        )
        for rel in relations:
            peer_id = (
                rel.target_object_type_id
                if rel.source_object_type_id == obj.id
                else rel.source_object_type_id
            )
            relation_peer_ids.add(peer_id)

        if relation_peer_ids:
            peers = db.query(ObjectType).filter(ObjectType.id.in_(relation_peer_ids)).all()
            for logic in logics:
                if any(_logic_relates_to_object(logic, peer) for peer in peers):
                    related.append(logic)
        # 去重
        seen: set[str] = set()
        unique: list[BusinessLogic] = []
        for logic in related:
            if logic.id not in seen:
                seen.add(logic.id)
                unique.append(logic)
        return unique

    def _count_related_logics(self, db: Session, obj: ObjectType) -> int:
        return len(self._related_logics_for_object(db, obj))

    def _related_objects_for_logic(
        self, db: Session, logic: BusinessLogic
    ) -> list[ObjectType]:
        object_ids = self._logic_object_binding_ids(db, logic)
        if object_ids:
            return (
                db.query(ObjectType)
                .filter(ObjectType.id.in_(object_ids))
                .order_by(ObjectType.display_name.asc())
                .all()
            )

        # 历史数据兜底
        objects = (
            db.query(ObjectType).filter(ObjectType.ontology_id == logic.ontology_id).all()
        )
        related = [obj for obj in objects if _object_relates_to_logic(obj, logic)]
        if related:
            return related

        relations = (
            db.query(RelationType).filter(RelationType.ontology_id == logic.ontology_id).all()
        )
        seen: set[str] = set()
        unique: list[ObjectType] = []
        for rel in relations:
            source = db.get(ObjectType, rel.source_object_type_id)
            target = db.get(ObjectType, rel.target_object_type_id)
            for cand in (source, target):
                if cand and _object_relates_to_logic(cand, logic) and cand.id not in seen:
                    seen.add(cand.id)
                    unique.append(cand)
        return unique

    def _logic_bindings_for_object(
        self, db: Session, obj: ObjectType
    ) -> list:
        from app.schemas import ObjectTypeLogicBindingOut

        rows = (
            db.query(BusinessLogicObjectBinding)
            .filter(BusinessLogicObjectBinding.object_type_id == obj.id)
            .order_by(BusinessLogicObjectBinding.created_at.desc())
            .all()
        )
        out: list[ObjectTypeLogicBindingOut] = []
        for b in rows:
            logic = db.get(BusinessLogic, b.business_logic_id)
            if not logic:
                continue
            out.append(
                ObjectTypeLogicBindingOut(
                    binding_id=b.id,
                    role=b.role,
                    source=b.source,
                    confidence=b.confidence,
                    logic_id=logic.id,
                    logic_name=logic.name,
                    logic_display_name=logic.display_name,
                    logic_type=logic.logic_type,
                    logic_status=logic.status,
                    created_at=b.created_at,
                )
            )
        return out

    def _related_properties_for_logic(
        self, db: Session, logic: BusinessLogic, objects: list[ObjectType]
    ) -> list[Property]:
        # 优先：显式字段绑定
        prop_ids = [
            r[0]
            for r in (
                db.query(BusinessLogicPropertyBinding.property_id)
                .filter(BusinessLogicPropertyBinding.business_logic_id == logic.id)
                .distinct()
                .all()
            )
        ]
        if prop_ids:
            return db.query(Property).filter(Property.id.in_(prop_ids)).all()

        # 兜底：文本命中
        blob = _logic_text_blob(logic)
        props: list[Property] = []
        for obj in objects:
            for prop in obj.properties:
                if prop.name.lower() in blob or prop.display_name.lower() in blob:
                    props.append(prop)
        return props

    def _object_bindings_for_logic(
        self, db: Session, logic: BusinessLogic
    ) -> list[BusinessLogicObjectBindingOut]:
        rows = (
            db.query(BusinessLogicObjectBinding)
            .filter(BusinessLogicObjectBinding.business_logic_id == logic.id)
            .all()
        )
        out: list[BusinessLogicObjectBindingOut] = []
        for b in rows:
            obj = db.get(ObjectType, b.object_type_id)
            out.append(
                BusinessLogicObjectBindingOut(
                    id=b.id,
                    business_logic_id=b.business_logic_id,
                    object_type_id=b.object_type_id,
                    object_type_name=obj.name if obj else None,
                    object_type_display_name=obj.display_name if obj else None,
                    role=b.role,
                    source=b.source,
                    confidence=b.confidence,
                    created_at=b.created_at,
                )
            )
        return out

    def _property_bindings_for_logic(
        self, db: Session, logic: BusinessLogic
    ) -> list[BusinessLogicPropertyBindingOut]:
        rows = (
            db.query(BusinessLogicPropertyBinding)
            .filter(BusinessLogicPropertyBinding.business_logic_id == logic.id)
            .all()
        )
        out: list[BusinessLogicPropertyBindingOut] = []
        for b in rows:
            prop = db.get(Property, b.property_id)
            obj = db.get(ObjectType, prop.object_type_id) if prop else None
            out.append(
                BusinessLogicPropertyBindingOut(
                    id=b.id,
                    business_logic_id=b.business_logic_id,
                    property_id=b.property_id,
                    property_name=prop.name if prop else None,
                    property_display_name=prop.display_name if prop else None,
                    object_type_id=obj.id if obj else None,
                    object_type_name=obj.name if obj else None,
                    role=b.role,
                    source=b.source,
                    confidence=b.confidence,
                    created_at=b.created_at,
                )
            )
        return out

    def list_versions_for_entity(self, db: Session, entity_id: str) -> list[VersionRecordOut]:
        return self.list_versions(db, entity_id)

    def list_ontologies(
        self,
        db: Session,
        domain_context_id: str | None = None,
        published_only: bool = False,
    ) -> list[OntologySummary]:
        query = db.query(Ontology)
        if domain_context_id:
            query = query.filter(Ontology.domain_context_id == domain_context_id)
        if published_only:
            query = query.filter(Ontology.status == OntologyStatus.PUBLISHED.value)
        ontologies = query.order_by(Ontology.updated_at.desc()).all()
        return [self._to_ontology_summary(db, o) for o in ontologies]

    def get_ontology(self, db: Session, ontology_id: str) -> OntologySummary | None:
        ontology = db.get(Ontology, ontology_id)
        if not ontology:
            return None
        return self._to_ontology_summary(db, ontology)

    def list_object_types(
        self,
        db: Session,
        ontology_id: str | None = None,
        domain_context_id: str | None = None,
        published_only: bool = False,
    ) -> list[ObjectTypeSummary]:
        query = db.query(ObjectType)
        query = self._apply_ontology_scope(
            db,
            query,
            ontology_id=ontology_id,
            domain_context_id=domain_context_id,
            published_only=published_only,
        )
        objects = query.order_by(ObjectType.updated_at.desc()).all()
        return [self._to_object_summary(db, obj) for obj in objects]

    def list_relation_types(
        self,
        db: Session,
        ontology_id: str | None = None,
        domain_context_id: str | None = None,
        published_only: bool = False,
    ) -> list[RelationTypeOut]:
        query = db.query(RelationType)
        query = self._apply_ontology_scope(
            db,
            query,
            ontology_id=ontology_id,
            domain_context_id=domain_context_id,
            published_only=published_only,
            ontology_model=RelationType,
        )
        relations = query.order_by(RelationType.updated_at.desc()).all()
        return [self._to_relation_out(db, rel) for rel in relations]

    def get_object_type(self, db: Session, object_type_id: str) -> ObjectTypeDetail | None:
        obj = (
            db.query(ObjectType)
            .options(joinedload(ObjectType.properties))
            .filter(ObjectType.id == object_type_id)
            .first()
        )
        if not obj:
            return None

        outgoing = (
            db.query(RelationType)
            .filter(RelationType.source_object_type_id == object_type_id)
            .all()
        )
        incoming = (
            db.query(RelationType)
            .filter(RelationType.target_object_type_id == object_type_id)
            .all()
        )
        related_logics = self._related_logics_for_object(db, obj)
        logic_bindings = self._logic_bindings_for_object(db, obj)

        summary = self._to_object_summary(db, obj)
        source_ref, datahub_url = self._resolve_object_datahub(db, obj)
        domain_id, domain_name = self._resolve_domain_context(db, obj.ontology_id)
        versions = self.list_versions(db, obj.id)
        ontology_versions = self.list_versions(db, obj.ontology_id)
        return ObjectTypeDetail(
            **summary.model_dump(),
            ontology_id=obj.ontology_id,
            domain_context_id=domain_id,
            domain_name=domain_name,
            source_ref=source_ref,
            datahub_url=datahub_url,
            properties=[PropertyOut.model_validate(p) for p in obj.properties],
            outgoing_relations=[self._to_relation_out(db, r) for r in outgoing],
            incoming_relations=[self._to_relation_out(db, r) for r in incoming],
            business_logics=[
                self._to_business_logic_out(db, logic) for logic in related_logics
            ],
            business_logic_bindings=logic_bindings,
            version_records=versions + ontology_versions,
        )

    def list_business_logics(
        self,
        db: Session,
        ontology_id: str | None = None,
        domain_context_id: str | None = None,
        published_only: bool = False,
    ) -> list[BusinessLogicOut]:
        query = db.query(BusinessLogic)
        query = self._apply_ontology_scope(
            db,
            query,
            ontology_id=ontology_id,
            domain_context_id=domain_context_id,
            published_only=published_only,
            ontology_model=BusinessLogic,
        )
        items = query.order_by(BusinessLogic.updated_at.desc()).all()
        return [self._to_business_logic_out(db, item) for item in items]

    def get_relation_type(self, db: Session, relation_type_id: str) -> RelationTypeDetail | None:
        rel = db.get(RelationType, relation_type_id)
        if not rel:
            return None

        source = db.get(ObjectType, rel.source_object_type_id)
        target = db.get(ObjectType, rel.target_object_type_id)
        mapping = db.get(ObjectType, rel.mapping_object_type_id) if rel.mapping_object_type_id else None
        base = self._to_relation_out(db, rel)

        source_object = None
        if source:
            source_ref, source_url = self._resolve_object_datahub(db, source)
            source_object = RelationObjectRef(
                id=source.id,
                name=source.name,
                display_name=source.display_name,
                source_ref=source_ref,
                datahub_url=source_url,
            )

        target_object = None
        if target:
            target_ref, target_url = self._resolve_object_datahub(db, target)
            target_object = RelationObjectRef(
                id=target.id,
                name=target.name,
                display_name=target.display_name,
                source_ref=target_ref,
                datahub_url=target_url,
            )

        mapping_object = None
        if mapping:
            mapping_ref, mapping_url = self._resolve_object_datahub(db, mapping)
            mapping_object = RelationObjectRef(
                id=mapping.id,
                name=mapping.name,
                display_name=mapping.display_name,
                source_ref=mapping_ref,
                datahub_url=mapping_url,
            )

        return RelationTypeDetail(
            **base.model_dump(),
            ontology_id=rel.ontology_id,
            source_object=source_object,
            target_object=target_object,
            mapping_object=mapping_object,
        )

    def get_business_logic(self, db: Session, logic_id: str) -> BusinessLogicDetail | None:
        logic = (
            db.query(BusinessLogic)
            .filter(BusinessLogic.id == logic_id)
            .first()
        )
        if not logic:
            return None

        related_objects = self._related_objects_for_logic(db, logic)
        related_objects_with_props = (
            db.query(ObjectType)
            .options(joinedload(ObjectType.properties))
            .filter(ObjectType.id.in_([o.id for o in related_objects]))
            .all()
            if related_objects
            else []
        )
        related_properties = self._related_properties_for_logic(
            db, logic, related_objects_with_props
        )
        object_bindings = self._object_bindings_for_logic(db, logic)
        property_bindings = self._property_bindings_for_logic(db, logic)
        versions = self.list_versions(db, logic.id)
        ontology_versions = self.list_versions(db, logic.ontology_id)

        return BusinessLogicDetail(
            **self._to_business_logic_out(db, logic).model_dump(),
            related_object_types=[
                self._to_object_summary(db, obj) for obj in related_objects
            ],
            related_properties=[PropertyOut.model_validate(p) for p in related_properties],
            object_bindings=object_bindings,
            property_bindings=property_bindings,
            version_records=versions + ontology_versions,
        )

    def get_ontology_graph(self, db: Session, ontology_id: str) -> OntologyGraph:
        objects = db.query(ObjectType).filter(ObjectType.ontology_id == ontology_id).all()
        relations = db.query(RelationType).filter(RelationType.ontology_id == ontology_id).all()
        nodes = [
            GraphNode(id=obj.id, label=obj.name, display_name=obj.display_name, status=obj.status)
            for obj in objects
        ]
        edges = [
            GraphEdge(
                id=rel.id,
                source=rel.source_object_type_id,
                target=rel.target_object_type_id,
                label=rel.display_name,
                cardinality=_normalize_cardinality(rel.cardinality),
                relation_id=rel.id,
            )
            for rel in relations
        ]
        return OntologyGraph(nodes=nodes, edges=edges)

    def list_versions(self, db: Session, entity_id: str) -> list[VersionRecordOut]:
        records = (
            db.query(VersionRecord)
            .filter(VersionRecord.entity_id == entity_id)
            .order_by(VersionRecord.version.desc())
            .all()
        )
        return [VersionRecordOut.model_validate(r) for r in records]

    def _to_ontology_summary(self, db: Session, ontology: Ontology) -> OntologySummary:
        return OntologySummary(
            id=ontology.id,
            domain_context_id=ontology.domain_context_id,
            version=ontology.version,
            status=ontology.status,
            generated_at=ontology.generated_at,
            published_at=ontology.published_at,
            object_type_count=db.query(ObjectType).filter(ObjectType.ontology_id == ontology.id).count(),
            relation_type_count=db.query(RelationType)
            .filter(RelationType.ontology_id == ontology.id)
            .count(),
            business_logic_count=db.query(BusinessLogic)
            .filter(BusinessLogic.ontology_id == ontology.id)
            .count(),
        )

    def _to_object_summary(self, db: Session, obj: ObjectType) -> ObjectTypeSummary:
        property_count = db.query(Property).filter(Property.object_type_id == obj.id).count()
        relation_count = (
            db.query(RelationType)
            .filter(
                (RelationType.source_object_type_id == obj.id)
                | (RelationType.target_object_type_id == obj.id)
            )
            .count()
        )
        logic_count = self._count_related_logics(db, obj)
        bound_logic_count = (
            db.query(BusinessLogicObjectBinding)
            .filter(BusinessLogicObjectBinding.object_type_id == obj.id)
            .distinct(BusinessLogicObjectBinding.business_logic_id)
            .count()
        )
        return ObjectTypeSummary(
            id=obj.id,
            name=obj.name,
            display_name=obj.display_name,
            description=obj.description,
            status=obj.status,
            property_count=property_count,
            relation_count=relation_count,
            business_logic_count=logic_count,
            bound_logic_count=bound_logic_count,
            source_confidence=obj.source_confidence,
            updated_at=obj.updated_at,
        )

    def _to_relation_out(self, db: Session, rel: RelationType) -> RelationTypeOut:
        source = db.get(ObjectType, rel.source_object_type_id)
        target = db.get(ObjectType, rel.target_object_type_id)
        return RelationTypeOut(
            id=rel.id,
            name=rel.name,
            display_name=rel.display_name,
            description=rel.description,
            source_object_type_id=rel.source_object_type_id,
            target_object_type_id=rel.target_object_type_id,
            source_object_name=source.display_name if source else None,
            target_object_name=target.display_name if target else None,
            cardinality=_normalize_cardinality(rel.cardinality),
            structure_type=rel.structure_type
            or infer_relation_structure_type(rel.description, rel.source_evidence),
            mapping_object_type_id=rel.mapping_object_type_id,
            mapping_object_name=rel.mapping_object_type.display_name
            if rel.mapping_object_type
            else None,
            source_evidence=rel.source_evidence,
            status=rel.status,
            source_confidence=rel.source_confidence,
        )

    def _resolve_object_datahub(
        self, db: Session, obj: ObjectType
    ) -> tuple[str | None, str | None]:
        from app.connectors.datahub import DataHubConnector
        from app.services.settings_service import SettingsService

        datahub = DataHubConnector(SettingsService().get_datahub_runtime(db))
        if obj.source_ref:
            return obj.source_ref, datahub.get_dataset_url(obj.source_ref)

        name_hint = obj.name.replace("_entity", "")
        evidences = (
            db.query(DraftEvidence)
            .filter(
                DraftEvidence.ontology_id == obj.ontology_id,
                DraftEvidence.source_ref.like("urn:li:dataset:%"),
            )
            .all()
        )
        for ev in evidences:
            urn = ev.source_ref.split("#")[0]
            if name_hint in urn or obj.name in urn:
                return urn, datahub.get_dataset_url(urn)

        for ev in evidences:
            urn = ev.source_ref.split("#")[0]
            parts = urn.split(",")
            if len(parts) >= 2:
                table_name = parts[1].rstrip(")")
                if table_name == name_hint or table_name.endswith(f".{name_hint}"):
                    return urn, datahub.get_dataset_url(urn)

        return None, None


class WorkspaceService:
    """工作区：数据域同步与草稿生成。"""

    def __init__(self) -> None:
        from app.services.evidence_builder import EvidenceBuilder
        from app.services.publish import DraftPersistenceService
        from app.services.settings_service import SettingsService

        self.settings_service = SettingsService()
        self.evidence_builder = EvidenceBuilder()
        self.persistence = DraftPersistenceService()

    def _datahub(self, db: Session):
        from app.connectors.datahub import DataHubConnector

        return DataHubConnector(self.settings_service.get_datahub_runtime(db))

    def _draft_generator(self, db: Session):
        from app.services.draft_generator import OntologyDraftGenerator

        return OntologyDraftGenerator(self.settings_service.get_llm_runtime(db))

    async def sync_domains(self, db: Session) -> list[DomainContextSummary]:
        domains = await self._datahub(db).list_domains()
        for domain in domains:
            existing = (
                db.query(DomainContext)
                .filter(DomainContext.datahub_domain_id == domain.id)
                .first()
            )
            if existing:
                existing.name = domain.name
                existing.description = domain.description
                existing.owner = domain.owner
            else:
                db.add(
                    DomainContext(
                        datahub_domain_id=domain.id,
                        name=domain.name,
                        description=domain.description,
                        owner=domain.owner,
                    )
                )
        db.commit()
        return self.list_domains(db)

    def list_domains(self, db: Session) -> list[DomainContextSummary]:
        domains = db.query(DomainContext).order_by(DomainContext.updated_at.desc()).all()
        result: list[DomainContextSummary] = []
        for domain in domains:
            draft_count = (
                db.query(Ontology)
                .filter(
                    Ontology.domain_context_id == domain.id,
                    Ontology.status.in_([OntologyStatus.DRAFT.value, OntologyStatus.IN_REVIEW.value]),
                )
                .count()
            )
            published_count = (
                db.query(Ontology)
                .filter(
                    Ontology.domain_context_id == domain.id,
                    Ontology.status == OntologyStatus.PUBLISHED.value,
                )
                .count()
            )
            latest_draft = (
                db.query(Ontology)
                .filter(
                    Ontology.domain_context_id == domain.id,
                    Ontology.status.in_([OntologyStatus.DRAFT.value, OntologyStatus.IN_REVIEW.value]),
                )
                .order_by(Ontology.updated_at.desc())
                .first()
            )
            latest_published = (
                db.query(Ontology)
                .filter(
                    Ontology.domain_context_id == domain.id,
                    Ontology.status == OntologyStatus.PUBLISHED.value,
                )
                .order_by(Ontology.published_at.desc())
                .first()
            )
            latest = (
                db.query(Ontology)
                .filter(Ontology.domain_context_id == domain.id)
                .order_by(Ontology.updated_at.desc())
                .first()
            )
            domain_status = latest.status if latest else "active"
            result.append(
                DomainContextSummary(
                    id=domain.id,
                    datahub_domain_id=domain.datahub_domain_id,
                    name=domain.name,
                    description=domain.description,
                    owner=domain.owner,
                    status=domain_status,
                    draft_count=draft_count,
                    published_count=published_count,
                    latest_draft_at=latest_draft.updated_at if latest_draft else None,
                    latest_published_at=latest_published.published_at if latest_published else None,
                    updated_at=domain.updated_at,
                )
            )
        return result

    def get_domain(self, db: Session, domain_id: str) -> DomainContextDetail | None:
        domain = db.get(DomainContext, domain_id)
        if not domain:
            return None
        summary = next((d for d in self.list_domains(db) if d.id == domain_id), None)
        if not summary:
            return None
        latest = (
            db.query(Ontology)
            .filter(Ontology.domain_context_id == domain_id)
            .order_by(Ontology.updated_at.desc())
            .first()
        )
        published = OntologyQueryService().get_published_ontology(db, domain_id)
        datahub = self._datahub(db)
        return DomainContextDetail(
            **summary.model_dump(),
            datahub_url=datahub.get_domain_url(domain.datahub_domain_id),
            latest_ontology_id=latest.id if latest else None,
            latest_ontology_status=latest.status if latest else None,
            published_ontology_id=published.id if published else None,
            published_ontology_version=published.version if published else None,
        )

    def start_draft_generation(self, db: Session, domain_id: str) -> DraftProgressOut:
        domain = db.get(DomainContext, domain_id)
        if not domain:
            raise ValueError("Domain not found")

        task = DraftGenerationTask(
            domain_context_id=domain_id,
            status="running",
            progress=0,
            message="准备生成本体草稿...",
        )
        db.add(task)
        db.commit()
        db.refresh(task)

        progress = DraftProgressOut(
            task_id=task.id,
            status=task.status,
            progress=task.progress,
            message=task.message,
        )

        return progress

    async def _run_draft_generation(self, domain_id: str, task_id: str) -> None:
        from app.database import SessionLocal

        db = SessionLocal()
        try:
            task = db.get(DraftGenerationTask, task_id)
            domain = db.get(DomainContext, domain_id)

            task.progress = 5
            task.message = "正在从 DataHub 拉取元数据..."
            db.commit()

            bundle = await self._datahub(db).fetch_domain_bundle(domain.datahub_domain_id)
            task.progress = 30
            task.message = "正在组装证据包..."
            db.commit()

            evidence = self.evidence_builder.build(bundle)
            task.progress = 55
            task.message = "正在生成本体草稿..."
            db.commit()

            draft = await self._draft_generator(db).generate(evidence)
            task.progress = 80
            task.message = "正在持久化草稿..."
            db.commit()

            purged = self._purge_draft_ontologies(db, domain_id)
            if purged:
                _log_change(
                    db,
                    "ontology",
                    domain_id,
                    "purge_draft",
                    summary=f"重新生成草稿前清理 {purged} 个旧草稿本体",
                )

            ontology = Ontology(
                domain_context_id=domain_id,
                status=OntologyStatus.DRAFT.value,
                generated_by="llm",
            )
            db.add(ontology)
            db.flush()

            self.persistence.save_draft(db, ontology, draft)
            _log_change(db, "ontology", ontology.id, "generate_draft", summary="LLM 草稿生成")

            task.ontology_id = ontology.id
            task.status = "completed"
            task.progress = 100
            task.message = "草稿生成完成"
            db.commit()
        except Exception as exc:
            task = db.get(DraftGenerationTask, task_id)
            if task:
                task.status = "failed"
                task.message = str(exc)
                db.commit()
        finally:
            db.close()

    def _purge_draft_ontologies(self, db: Session, domain_id: str) -> int:
        """删除同域所有 draft 状态本体及其关联数据，返回删除的本体数。

        重新生成草稿时调用，确保每个数据域同一时刻至多一个 draft 本体，
        避免工作区卡片"草稿 N"数字随历史草稿生成次数累加。
        in_review / published / archived 状态的本体不受影响。
        """
        drafts = (
            db.query(Ontology)
            .filter(
                Ontology.domain_context_id == domain_id,
                Ontology.status == OntologyStatus.DRAFT.value,
            )
            .all()
        )
        if not drafts:
            return 0
        return self._delete_ontologies_cascade(db, [o.id for o in drafts])

    def _delete_ontologies_cascade(self, db: Session, ontology_ids: list[str]) -> int:
        """按依赖顺序级联删除指定本体及其所有关联数据，返回删除的本体数。

        EntityChangeLog 通过 entity_id 字符串（非外键）引用本体，保留作为审计历史。
        """
        if not ontology_ids:
            return 0

        object_type_ids = [
            ot.id
            for ot in db.query(ObjectType)
            .filter(ObjectType.ontology_id.in_(ontology_ids))
            .all()
        ]
        property_ids = (
            [
                p.id
                for p in db.query(Property)
                .filter(Property.object_type_id.in_(object_type_ids))
                .all()
            ]
            if object_type_ids
            else []
        )
        business_logic_ids = [
            bl.id
            for bl in db.query(BusinessLogic)
            .filter(BusinessLogic.ontology_id.in_(ontology_ids))
            .all()
        ]

        if property_ids or business_logic_ids:
            db.query(BusinessLogicPropertyBinding).filter(
                or_(
                    BusinessLogicPropertyBinding.property_id.in_(property_ids),
                    BusinessLogicPropertyBinding.business_logic_id.in_(business_logic_ids),
                )
            ).delete(synchronize_session=False)

        if object_type_ids or business_logic_ids:
            db.query(BusinessLogicObjectBinding).filter(
                or_(
                    BusinessLogicObjectBinding.object_type_id.in_(object_type_ids),
                    BusinessLogicObjectBinding.business_logic_id.in_(business_logic_ids),
                )
            ).delete(synchronize_session=False)

        db.query(BusinessLogic).filter(
            BusinessLogic.ontology_id.in_(ontology_ids)
        ).delete(synchronize_session=False)

        if object_type_ids:
            db.query(Property).filter(
                Property.object_type_id.in_(object_type_ids)
            ).delete(synchronize_session=False)

        db.query(RelationType).filter(
            RelationType.ontology_id.in_(ontology_ids)
        ).delete(synchronize_session=False)

        db.query(ObjectType).filter(
            ObjectType.ontology_id.in_(ontology_ids)
        ).delete(synchronize_session=False)

        db.query(DraftEvidence).filter(
            DraftEvidence.ontology_id.in_(ontology_ids)
        ).delete(synchronize_session=False)

        db.query(ChangeConfirmation).filter(
            ChangeConfirmation.ontology_id.in_(ontology_ids)
        ).delete(synchronize_session=False)

        db.query(DraftGenerationTask).filter(
            DraftGenerationTask.ontology_id.in_(ontology_ids)
        ).update(
            {DraftGenerationTask.ontology_id: None},
            synchronize_session=False,
        )

        db.query(Ontology).filter(Ontology.id.in_(ontology_ids)).delete(
            synchronize_session=False
        )
        db.flush()
        return len(ontology_ids)

    def get_progress(self, db: Session, domain_id: str) -> DraftProgressOut | None:
        task = (
            db.query(DraftGenerationTask)
            .filter(DraftGenerationTask.domain_context_id == domain_id)
            .order_by(DraftGenerationTask.created_at.desc())
            .first()
        )
        if not task:
            return None
        return DraftProgressOut(
            task_id=task.id,
            status=task.status,
            progress=task.progress,
            message=task.message,
            ontology_id=task.ontology_id,
        )

    def list_tasks(self, db: Session, domain_id: str) -> list[TaskRecordOut]:
        tasks = (
            db.query(DraftGenerationTask)
            .filter(DraftGenerationTask.domain_context_id == domain_id)
            .order_by(DraftGenerationTask.created_at.desc())
            .all()
        )
        return [TaskRecordOut.model_validate(task) for task in tasks]

    def get_task_logs(self, db: Session, domain_id: str, task_id: str) -> list[ChangeLogOut]:
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

        logs: list[ChangeLogOut] = []
        if task.ontology_id:
            records = (
                db.query(EntityChangeLog)
                .filter(EntityChangeLog.entity_id == task.ontology_id)
                .order_by(EntityChangeLog.created_at.asc())
                .all()
            )
            logs.extend(ChangeLogOut.model_validate(r) for r in records)

        if task.message:
            logs.insert(
                0,
                ChangeLogOut(
                    id=f"task-{task.id}",
                    entity_type="task",
                    entity_id=task.id,
                    action=task.status,
                    change_summary=task.message,
                    created_at=task.updated_at,
                ),
            )
        return logs
