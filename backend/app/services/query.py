from datetime import datetime, timezone
import json
import logging

from sqlalchemy import or_, func
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
    BusinessLogicPropertyOption,
    BusinessLogicRef,
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
from app.services.common import log_change

logger = logging.getLogger("ontometa.workspace")

# 持有后台草稿生成任务的强引用，避免 asyncio 在任务完成前将其 GC 回收。
_background_tasks: set = set()


def _loads_json(value: str | None) -> dict | None:
    if not value:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def _log_change(
    db: Session,
    entity_type: str,
    entity_id: str,
    action: str,
    operator: str | None = None,
    summary: str | None = None,
) -> None:
    # 保留以兼容内部调用，统一委托到 app.services.common.log_change
    log_change(db, entity_type, entity_id, action, operator, summary)


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


def _logic_referenced_ids(logic: BusinessLogic) -> tuple[set[str], set[str]]:
    """从业务逻辑的表达式中解析出引用过的 (object_type_ids, property_ids)。

    判定来源优先级：expression_json > expression_draft。两者都会扫描。
    - expression_json: {"refs": [{"object_type_id": ..., "property_id": ...}, ...]}
    - expression_draft: {"segments": [{"type": "ref", "object_type_id": ..., "property_id": ...}, ...]}

    业务逻辑计算中引用过该本体下的对象/字段，即视为"绑定"。
    """
    obj_ids: set[str] = set()
    prop_ids: set[str] = set()
    for raw in (logic.expression_json, logic.expression_draft):
        if not raw:
            continue
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        refs = data.get("refs")
        if isinstance(refs, list):
            for r in refs:
                if not isinstance(r, dict):
                    continue
                oid = r.get("object_type_id")
                pid = r.get("property_id")
                if oid:
                    obj_ids.add(oid)
                if pid:
                    prop_ids.add(pid)
        segments = data.get("segments")
        if isinstance(segments, list):
            for seg in segments:
                if not isinstance(seg, dict) or seg.get("type") != "ref":
                    continue
                oid = seg.get("object_type_id")
                pid = seg.get("property_id")
                if oid:
                    obj_ids.add(oid)
                if pid:
                    prop_ids.add(pid)
    return obj_ids, prop_ids


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

    def _bulk_resolve_domain_context(
        self, db: Session, ontology_ids: list[str]
    ) -> dict[str, tuple[str | None, str | None]]:
        """一次性解析多个 ontology_id -> (domain_id, domain_name)。"""
        if not ontology_ids:
            return {}
        rows = (
            db.query(Ontology.id, Ontology.domain_context_id, DomainContext.id, DomainContext.name)
            .outerjoin(DomainContext, Ontology.domain_context_id == DomainContext.id)
            .filter(Ontology.id.in_(ontology_ids))
            .all()
        )
        return {
            oid: (did or None, dname) for oid, _, did, dname in rows
        }

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
        self,
        db: Session,
        logic: BusinessLogic,
        *,
        domain: tuple[str | None, str | None] | None = None,
        binding_counts: tuple[int, int] | None = None,
    ) -> BusinessLogicOut:
        if domain is None:
            domain_id, domain_name = self._resolve_domain_context(db, logic.ontology_id)
        else:
            domain_id, domain_name = domain
        if binding_counts is None:
            binding_counts = self._bulk_business_logic_binding_counts(db, [logic.id]).get(
                logic.id, (0, 0)
            )
        bound_object_count, bound_property_count = binding_counts
        return BusinessLogicOut(
            id=logic.id,
            name=logic.name,
            display_name=logic.display_name,
            logic_type=logic.logic_type,
            description=logic.description,
            expression_summary=logic.expression_summary,
            expression_draft=_loads_json(logic.expression_draft),
            expression_json=_loads_json(logic.expression_json),
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

    def _bulk_business_logic_binding_counts(
        self, db: Session, logic_ids: list[str]
    ) -> dict[str, tuple[int, int]]:
        """批量计算每个 business_logic 的 (bound_object_count, bound_property_count)。

        计数 = 显式绑定 ∪ 表达式引用（expression_json/expression_draft 中的 refs）
        去重后的对象/字段数。
        """
        if not logic_ids:
            return {}
        logics = (
            db.query(BusinessLogic)
            .filter(BusinessLogic.id.in_(logic_ids))
            .all()
        )
        obj_binding_rows = (
            db.query(
                BusinessLogicObjectBinding.business_logic_id,
                BusinessLogicObjectBinding.object_type_id,
            )
            .filter(BusinessLogicObjectBinding.business_logic_id.in_(logic_ids))
            .distinct()
            .all()
        )
        prop_binding_rows = (
            db.query(
                BusinessLogicPropertyBinding.business_logic_id,
                BusinessLogicPropertyBinding.property_id,
            )
            .filter(
                BusinessLogicPropertyBinding.business_logic_id.in_(logic_ids),
                BusinessLogicPropertyBinding.property_id.isnot(None),
            )
            .distinct()
            .all()
        )
        obj_bound: dict[str, set[str]] = {lid: set() for lid in logic_ids}
        prop_bound: dict[str, set[str]] = {lid: set() for lid in logic_ids}
        for lid, oid in obj_binding_rows:
            obj_bound[lid].add(oid)
        for lid, pid in prop_binding_rows:
            prop_bound[lid].add(pid)
        ref_obj: dict[str, set[str]] = {lid: set() for lid in logic_ids}
        ref_prop: dict[str, set[str]] = {lid: set() for lid in logic_ids}
        for logic in logics:
            oids, pids = _logic_referenced_ids(logic)
            ref_obj[logic.id] |= oids
            ref_prop[logic.id] |= pids
        return {
            lid: (
                len(obj_bound[lid] | ref_obj[lid]),
                len(prop_bound[lid] | ref_prop[lid]),
            )
            for lid in logic_ids
        }

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
        """返回与该对象关联的业务逻辑。

        关联判定 = 显式绑定（BusinessLogicObjectBinding）
                ∪ 表达式引用（expression_json/expression_draft 中 refs 引用了该对象）。
        """
        bound_ids = set(self._object_logic_ids(db, obj))
        referenced_ids = self._object_referenced_logic_map(db, [obj.id]).get(obj.id, set())
        all_ids = bound_ids | referenced_ids
        if not all_ids:
            return []
        return (
            db.query(BusinessLogic)
            .filter(BusinessLogic.id.in_(all_ids))
            .order_by(BusinessLogic.updated_at.desc())
            .all()
        )

    def _object_referenced_logic_map(
        self, db: Session, object_ids: list[str]
    ) -> dict[str, set[str]]:
        """返回 object_type_id -> {business_logic_id} 基于表达式引用。

        仅扫描传入对象所属本体下的业务逻辑，避免全表扫描。
        """
        if not object_ids:
            return {}
        rows = (
            db.query(ObjectType.id, ObjectType.ontology_id)
            .filter(ObjectType.id.in_(object_ids))
            .all()
        )
        obj_to_ontology = {oid: oid_ for oid, oid_ in rows}
        if not obj_to_ontology:
            return {oid: set() for oid in object_ids}
        ontology_ids = set(obj_to_ontology.values())
        logics = (
            db.query(BusinessLogic)
            .filter(BusinessLogic.ontology_id.in_(list(ontology_ids)))
            .all()
        )
        result: dict[str, set[str]] = {oid: set() for oid in object_ids}
        for logic in logics:
            ref_obj_ids, _ = _logic_referenced_ids(logic)
            for oid in ref_obj_ids:
                if oid in result:
                    result[oid].add(logic.id)
        return result

    def _related_objects_for_logic(
        self, db: Session, logic: BusinessLogic
    ) -> list[ObjectType]:
        """返回与该业务逻辑关联的对象。

        关联判定 = 显式绑定 ∪ 表达式引用（refs 中提到的对象）。
        """
        bound_ids = set(self._logic_object_binding_ids(db, logic))
        ref_obj_ids, _ = _logic_referenced_ids(logic)
        all_ids = bound_ids | ref_obj_ids
        if not all_ids:
            return []
        return (
            db.query(ObjectType)
            .filter(ObjectType.id.in_(all_ids))
            .order_by(ObjectType.display_name.asc())
            .all()
        )

    def _logic_bindings_for_object(
        self, db: Session, obj: ObjectType
    ) -> list:
        from app.schemas import ObjectTypeLogicBindingOut

        rows = (
            db.query(BusinessLogicObjectBinding, BusinessLogic)
            .join(
                BusinessLogic,
                BusinessLogic.id == BusinessLogicObjectBinding.business_logic_id,
            )
            .filter(BusinessLogicObjectBinding.object_type_id == obj.id)
            .order_by(BusinessLogicObjectBinding.created_at.desc())
            .all()
        )
        return [
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
            for b, logic in rows
            if logic is not None
        ]

    def _related_properties_for_logic(
        self, db: Session, logic: BusinessLogic, objects: list[ObjectType]
    ) -> list[Property]:
        """返回与该业务逻辑关联的字段。

        关联判定 = 显式绑定（BusinessLogicPropertyBinding）∪ 表达式引用。
        objects 参数保留以兼容调用方签名，不再参与计算。
        """
        bound_ids = {
            r[0]
            for r in (
                db.query(BusinessLogicPropertyBinding.property_id)
                .filter(BusinessLogicPropertyBinding.business_logic_id == logic.id)
                .distinct()
                .all()
            )
        }
        _, ref_prop_ids = _logic_referenced_ids(logic)
        all_ids = bound_ids | ref_prop_ids
        if not all_ids:
            return []
        return db.query(Property).filter(Property.id.in_(all_ids)).all()

    def _object_bindings_for_logic(
        self, db: Session, logic: BusinessLogic
    ) -> list[BusinessLogicObjectBindingOut]:
        rows = (
            db.query(BusinessLogicObjectBinding, ObjectType)
            .outerjoin(
                ObjectType,
                ObjectType.id == BusinessLogicObjectBinding.object_type_id,
            )
            .filter(BusinessLogicObjectBinding.business_logic_id == logic.id)
            .order_by(BusinessLogicObjectBinding.created_at.desc())
            .all()
        )
        return [
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
            for b, obj in rows
        ]

    def _property_bindings_for_logic(
        self, db: Session, logic: BusinessLogic
    ) -> list[BusinessLogicPropertyBindingOut]:
        rows = (
            db.query(BusinessLogicPropertyBinding, Property, ObjectType)
            .outerjoin(
                Property, Property.id == BusinessLogicPropertyBinding.property_id
            )
            .outerjoin(ObjectType, ObjectType.id == Property.object_type_id)
            .filter(BusinessLogicPropertyBinding.business_logic_id == logic.id)
            .order_by(BusinessLogicPropertyBinding.created_at.desc())
            .all()
        )
        return [
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
            for b, prop, obj in rows
        ]

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
        if not ontologies:
            return []
        counts = self._bulk_ontology_entity_counts(db, [o.id for o in ontologies])
        return [
            self._to_ontology_summary(db, o, counts=counts.get(o.id, (0, 0, 0)))
            for o in ontologies
        ]

    def _bulk_ontology_entity_counts(
        self, db: Session, ontology_ids: list[str]
    ) -> dict[str, tuple[int, int, int]]:
        """批量返回 ontology_id -> (object_type_count, relation_type_count, business_logic_count)。"""
        if not ontology_ids:
            return {}
        object_rows = (
            db.query(ObjectType.ontology_id, func.count(ObjectType.id))
            .filter(ObjectType.ontology_id.in_(ontology_ids))
            .group_by(ObjectType.ontology_id)
            .all()
        )
        relation_rows = (
            db.query(RelationType.ontology_id, func.count(RelationType.id))
            .filter(RelationType.ontology_id.in_(ontology_ids))
            .group_by(RelationType.ontology_id)
            .all()
        )
        logic_rows = (
            db.query(BusinessLogic.ontology_id, func.count(BusinessLogic.id))
            .filter(BusinessLogic.ontology_id.in_(ontology_ids))
            .group_by(BusinessLogic.ontology_id)
            .all()
        )
        omap = {oid: c for oid, c in object_rows}
        rmap = {oid: c for oid, c in relation_rows}
        lmap = {oid: c for oid, c in logic_rows}
        return {oid: (omap.get(oid, 0), rmap.get(oid, 0), lmap.get(oid, 0)) for oid in ontology_ids}

    def get_ontology(self, db: Session, ontology_id: str) -> OntologySummary | None:
        ontology = db.get(Ontology, ontology_id)
        if not ontology:
            return None
        counts = self._bulk_ontology_entity_counts(db, [ontology_id]).get(
            ontology_id, (0, 0, 0)
        )
        return self._to_ontology_summary(db, ontology, counts=counts)

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
        if not objects:
            return []
        stats = self._bulk_object_stats(db, [o.id for o in objects])
        return [
            self._to_object_summary(db, obj, stats=stats.get(obj.id))
            for obj in objects
        ]

    def _bulk_object_stats(
        self, db: Session, object_ids: list[str]
    ) -> dict[str, tuple[int, int, int]]:
        """批量返回 object_type_id -> (property_count, relation_count, bound_logic_count)。

        bound_logic_count = 显式绑定（BusinessLogicObjectBinding）∪ 表达式引用
        （business_logic 的 expression_json/expression_draft 中 refs 提到该对象）
        的去重 logic 数。
        """
        if not object_ids:
            return {}
        property_rows = (
            db.query(Property.object_type_id, func.count(Property.id))
            .filter(Property.object_type_id.in_(object_ids))
            .group_by(Property.object_type_id)
            .all()
        )
        # 关联关系数：source 或 target 任一命中
        source_rows = (
            db.query(RelationType.source_object_type_id, func.count(RelationType.id))
            .filter(RelationType.source_object_type_id.in_(object_ids))
            .group_by(RelationType.source_object_type_id)
            .all()
        )
        target_rows = (
            db.query(RelationType.target_object_type_id, func.count(RelationType.id))
            .filter(RelationType.target_object_type_id.in_(object_ids))
            .group_by(RelationType.target_object_type_id)
            .all()
        )
        binding_rows = (
            db.query(
                BusinessLogicObjectBinding.object_type_id,
                BusinessLogicObjectBinding.business_logic_id,
            )
            .filter(BusinessLogicObjectBinding.object_type_id.in_(object_ids))
            .all()
        )
        pmap = {oid: c for oid, c in property_rows}
        smap = {oid: c for oid, c in source_rows}
        tmap = {oid: c for oid, c in target_rows}
        binding_map: dict[str, set[str]] = {oid: set() for oid in object_ids}
        for oid, lid in binding_rows:
            binding_map[oid].add(lid)
        referenced_map = self._object_referenced_logic_map(db, object_ids)
        return {
            oid: (
                pmap.get(oid, 0),
                smap.get(oid, 0) + tmap.get(oid, 0),
                len(binding_map[oid] | referenced_map.get(oid, set())),
            )
            for oid in object_ids
        }

    def list_relation_types(
        self,
        db: Session,
        ontology_id: str | None = None,
        domain_context_id: str | None = None,
        published_only: bool = False,
    ) -> list[RelationTypeOut]:
        query = db.query(RelationType).options(
            joinedload(RelationType.source_object_type),
            joinedload(RelationType.target_object_type),
            joinedload(RelationType.mapping_object_type),
        )
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
            .options(
                joinedload(RelationType.source_object_type),
                joinedload(RelationType.target_object_type),
                joinedload(RelationType.mapping_object_type),
            )
            .filter(RelationType.source_object_type_id == object_type_id)
            .all()
        )
        incoming = (
            db.query(RelationType)
            .options(
                joinedload(RelationType.source_object_type),
                joinedload(RelationType.target_object_type),
                joinedload(RelationType.mapping_object_type),
            )
            .filter(RelationType.target_object_type_id == object_type_id)
            .all()
        )
        related_logics = self._related_logics_for_object(db, obj)
        logic_bindings = self._logic_bindings_for_object(db, obj)

        # 详情与列表都基于显式绑定计数，二者保持一致。
        base_stats = self._bulk_object_stats(db, [obj.id]).get(obj.id, (0, 0, 0))
        full_stats = (base_stats[0], base_stats[1], len(related_logics))
        summary = self._to_object_summary(db, obj, stats=full_stats)
        related_logic_ids = [logic.id for logic in related_logics]
        related_domain_map = self._bulk_resolve_domain_context(
            db, [logic.ontology_id for logic in related_logics]
        )
        related_binding_map = self._bulk_business_logic_binding_counts(
            db, related_logic_ids
        )
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
                self._to_business_logic_out(
                    db,
                    logic,
                    domain=related_domain_map.get(logic.ontology_id, (None, None)),
                    binding_counts=related_binding_map.get(logic.id, (0, 0)),
                )
                for logic in related_logics
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
        if not items:
            return []
        logic_ids = [it.id for it in items]
        domain_map = self._bulk_resolve_domain_context(db, [it.ontology_id for it in items])
        binding_map = self._bulk_business_logic_binding_counts(db, logic_ids)
        return [
            self._to_business_logic_out(
                db,
                it,
                domain=domain_map.get(it.ontology_id, (None, None)),
                binding_counts=binding_map.get(it.id, (0, 0)),
            )
            for it in items
        ]

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
        logic = db.get(BusinessLogic, logic_id)
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

        available_object_types, available_properties = self._available_targets_for_logic(db, logic)

        related_object_logics = self._batch_object_logic_refs(db, [o.id for o in related_objects], exclude_logic_id=logic.id)

        return BusinessLogicDetail(
            **self._to_business_logic_out(db, logic).model_dump(),
            related_object_types=[
                self._to_object_summary(db, obj) for obj in related_objects
            ],
            related_object_logics=related_object_logics,
            related_properties=[PropertyOut.model_validate(p) for p in related_properties],
            object_bindings=object_bindings,
            property_bindings=property_bindings,
            version_records=versions + ontology_versions,
            ontology_id=logic.ontology_id,
            available_object_types=available_object_types,
            available_properties=available_properties,
        )

    def _available_targets_for_logic(
        self, db: Session, logic: BusinessLogic
    ) -> tuple[list[ObjectTypeSummary], list[BusinessLogicPropertyOption]]:
        """从业务逻辑所属本体(应为已发布本体)中取出可被引用的对象与字段候选。"""
        objects = (
            db.query(ObjectType)
            .filter(ObjectType.ontology_id == logic.ontology_id)
            .order_by(ObjectType.name)
            .all()
        )
        object_summaries = [self._to_object_summary(db, obj) for obj in objects]
        if not objects:
            return [], []

        obj_by_id = {obj.id: obj for obj in objects}
        properties = (
            db.query(Property)
            .filter(Property.object_type_id.in_(list(obj_by_id.keys())))
            .order_by(Property.object_type_id, Property.name)
            .all()
        )
        property_options = [
            BusinessLogicPropertyOption(
                property_id=prop.id,
                property_name=prop.name,
                property_display_name=prop.display_name,
                object_type_id=prop.object_type_id,
                object_type_name=obj_by_id[prop.object_type_id].name,
                object_type_display_name=obj_by_id[prop.object_type_id].display_name,
            )
            for prop in properties
        ]
        return object_summaries, property_options

    def _batch_object_logic_refs(
        self,
        db: Session,
        object_type_ids: list[str],
        exclude_logic_id: str | None = None,
    ) -> dict[str, list[BusinessLogicRef]]:
        """批量获取多个对象上绑定的业务逻辑引用，按 object_type_id 分组。"""
        if not object_type_ids:
            return {}
        rows = (
            db.query(BusinessLogicObjectBinding, BusinessLogic)
            .join(BusinessLogic, BusinessLogic.id == BusinessLogicObjectBinding.business_logic_id)
            .filter(BusinessLogicObjectBinding.object_type_id.in_(object_type_ids))
            .all()
        )
        result: dict[str, list[BusinessLogicRef]] = {oid: [] for oid in object_type_ids}
        for binding, logic in rows:
            if exclude_logic_id and logic.id == exclude_logic_id:
                continue
            ref = BusinessLogicRef(
                id=logic.id,
                name=logic.name,
                display_name=logic.display_name,
                logic_type=logic.logic_type,
                status=logic.status,
            )
            if ref not in result[binding.object_type_id]:
                result[binding.object_type_id].append(ref)
        return result

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

    def _to_ontology_summary(
        self,
        db: Session,
        ontology: Ontology,
        *,
        counts: tuple[int, int, int] | None = None,
    ) -> OntologySummary:
        if counts is None:
            counts = self._bulk_ontology_entity_counts(db, [ontology.id]).get(
                ontology.id, (0, 0, 0)
            )
        object_type_count, relation_type_count, business_logic_count = counts
        return OntologySummary(
            id=ontology.id,
            domain_context_id=ontology.domain_context_id,
            version=ontology.version,
            status=ontology.status,
            generated_at=ontology.generated_at,
            published_at=ontology.published_at,
            object_type_count=object_type_count,
            relation_type_count=relation_type_count,
            business_logic_count=business_logic_count,
        )

    def _to_object_summary(
        self,
        db: Session,
        obj: ObjectType,
        *,
        stats: tuple[int, int, int] | None = None,
    ) -> ObjectTypeSummary:
        if stats is None:
            stats = self._bulk_object_stats(db, [obj.id]).get(obj.id, (0, 0, 0))
        property_count, relation_count, bound_logic_count = stats
        logic_count = bound_logic_count
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
        # 优先使用已加载的关系属性，避免 N+1；未加载时回落到 db.get
        source = rel.source_object_type if "source_object_type" in rel.__dict__ else db.get(ObjectType, rel.source_object_type_id)
        target = rel.target_object_type if "target_object_type" in rel.__dict__ else db.get(ObjectType, rel.target_object_type_id)
        mapping = None
        if rel.mapping_object_type_id:
            mapping = rel.mapping_object_type if "mapping_object_type" in rel.__dict__ else db.get(ObjectType, rel.mapping_object_type_id)
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
            mapping_object_name=mapping.display_name if mapping else None,
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

    @staticmethod
    def _track_background_task(task) -> None:
        """持有后台 asyncio 任务强引用并在完成后自动清理。"""
        _background_tasks.add(task)

        def _done(_t, *_args):
            _background_tasks.discard(_t)

        task.add_done_callback(_done)

    async def sync_domains(self, db: Session) -> list[DomainContextSummary]:
        connector = self._datahub(db)
        try:
            domains = await connector.list_domains()
        except Exception:
            logger.warning("无法连接 DataHub 同步数据域，将返回本地缓存数据", exc_info=True)
            return self.list_domains(db)
        finally:
            await connector.aclose()
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
        if not domains:
            return []
        domain_ids = [d.id for d in domains]

        draft_statuses = [OntologyStatus.DRAFT.value, OntologyStatus.IN_REVIEW.value]

        # 一次性聚合 draft / published 数量
        draft_rows = (
            db.query(
                Ontology.domain_context_id,
                func.count(Ontology.id),
                func.max(Ontology.updated_at),
            )
            .filter(
                Ontology.domain_context_id.in_(domain_ids),
                Ontology.status.in_(draft_statuses),
            )
            .group_by(Ontology.domain_context_id)
            .all()
        )
        published_rows = (
            db.query(
                Ontology.domain_context_id,
                func.count(Ontology.id),
                func.max(Ontology.published_at),
            )
            .filter(
                Ontology.domain_context_id.in_(domain_ids),
                Ontology.status == OntologyStatus.PUBLISHED.value,
            )
            .group_by(Ontology.domain_context_id)
            .all()
        )
        draft_map = {did: (cnt, latest) for did, cnt, latest in draft_rows}
        published_map = {did: (cnt, latest) for did, cnt, latest in published_rows}

        # 每个 domain 的最新本体状态：单查询取所有域的本体，Python 端取最大 updated_at
        status_by_domain: dict[str, str] = {}
        status_rows = (
            db.query(
                Ontology.domain_context_id,
                Ontology.updated_at,
                Ontology.status,
            )
            .filter(Ontology.domain_context_id.in_(domain_ids))
            .all()
        )
        best: dict[str, tuple] = {}
        for did, updated_at, status in status_rows:
            prev = best.get(did)
            if prev is None or updated_at > prev[0]:
                best[did] = (updated_at, status)
        status_by_domain = {did: st for did, (_, st) in best.items()}

        result: list[DomainContextSummary] = []
        for domain in domains:
            draft_count, latest_draft_at = draft_map.get(domain.id, (0, None))
            published_count, latest_published_at = published_map.get(domain.id, (0, None))
            domain_status = status_by_domain.get(domain.id, "active")
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
                    latest_draft_at=latest_draft_at,
                    latest_published_at=latest_published_at,
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
        task: DraftGenerationTask | None = None
        try:
            task = db.get(DraftGenerationTask, task_id)
            if not task:
                logger.exception("DraftGenerationTask %s not found", task_id)
                return
            domain = db.get(DomainContext, domain_id)
            if not domain:
                task.status = "failed"
                task.message = "数据域不存在"
                db.commit()
                return

            task.progress = 5
            task.message = "正在从 DataHub 拉取元数据..."
            db.commit()

            connector = self._datahub(db)
            try:
                bundle = await connector.fetch_domain_bundle(domain.datahub_domain_id)
            finally:
                await connector.aclose()
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
            logger.exception("Draft generation failed for task %s: %s", task_id, exc)
            try:
                # task 可能在 db.get 之前就抛错，此时重新加载
                current = task if task is not None else db.get(DraftGenerationTask, task_id)
                if current is not None:
                    current.status = "failed"
                    current.message = str(exc)
                    db.commit()
            except Exception:
                logger.exception("Failed to mark task %s as failed", task_id)
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
