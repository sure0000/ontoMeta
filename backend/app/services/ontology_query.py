import json

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.models import (
    BusinessLogic,
    BusinessLogicObjectBinding,
    BusinessLogicPropertyBinding,
    DomainContext,
    DraftEvidence,
    ObjectType,
    Ontology,
    OntologyStatus,
    Property,
    RelationType,
    VersionRecord,
)
from app.schemas import (
    GraphEdge,
    GraphNode,
    ObjectTypeSummary,
    OntologyGraph,
    OntologySummary,
    PageResult,
    RelationObjectRef,
    RelationTypeDetail,
    RelationTypeOut,
    VersionRecordOut,
)
from app.services.relation_structure import infer_relation_structure_type

# 图谱局部展开默认节点上限（避免一次渲染全图）
_DEFAULT_GRAPH_MAX_NODES = 80

def _loads_json(value: str | None) -> dict | None:
    if not value:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None



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

class OntologyQueryService:
    """只读查询服务（本体 / 对象 / 关系）。"""

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
        *,
        q: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> PageResult[ObjectTypeSummary]:
        query = db.query(ObjectType)
        query = self._apply_ontology_scope(
            db,
            query,
            ontology_id=ontology_id,
            domain_context_id=domain_context_id,
            published_only=published_only,
        )
        if q and q.strip():
            like = f"%{q.strip()}%"
            query = query.filter(
                (ObjectType.name.ilike(like))
                | (ObjectType.display_name.ilike(like))
                | (ObjectType.description.ilike(like))
            )
        total = query.count()
        query = query.order_by(ObjectType.updated_at.desc())
        if offset:
            query = query.offset(max(0, offset))
        if limit is not None:
            query = query.limit(max(0, limit))
        objects = query.all()
        if not objects:
            return PageResult(items=[], total=total, limit=limit, offset=offset)
        stats = self._bulk_object_stats(db, [o.id for o in objects])
        domain_map = self._bulk_resolve_domain_context(
            db, [obj.ontology_id for obj in objects]
        )
        items = [
            self._to_object_summary(
                db,
                obj,
                stats=stats.get(obj.id),
                domain=domain_map.get(obj.ontology_id),
            )
            for obj in objects
        ]
        return PageResult(items=items, total=total, limit=limit, offset=offset)

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
        *,
        q: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> PageResult[RelationTypeOut]:
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
        if q and q.strip():
            like = f"%{q.strip()}%"
            query = query.filter(
                (RelationType.name.ilike(like))
                | (RelationType.display_name.ilike(like))
                | (RelationType.description.ilike(like))
            )
        total = query.count()
        query = query.order_by(RelationType.updated_at.desc())
        if offset:
            query = query.offset(max(0, offset))
        if limit is not None:
            query = query.limit(max(0, limit))
        relations = query.all()
        items = [self._to_relation_out(db, rel) for rel in relations]
        return PageResult(items=items, total=total, limit=limit, offset=offset)


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

    def get_ontology_graph(
        self,
        db: Session,
        ontology_id: str,
        *,
        center_id: str | None = None,
        depth: int = 1,
        full: bool = False,
        max_nodes: int = _DEFAULT_GRAPH_MAX_NODES,
    ) -> OntologyGraph:
        objects = db.query(ObjectType).filter(ObjectType.ontology_id == ontology_id).all()
        relations = db.query(RelationType).filter(RelationType.ontology_id == ontology_id).all()
        total_object_count = len(objects)
        total_relation_count = len(relations)
        obj_by_id = {obj.id: obj for obj in objects}

        if full or total_object_count <= max_nodes:
            nodes = [
                GraphNode(
                    id=obj.id, label=obj.name, display_name=obj.display_name, status=obj.status
                )
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
            return OntologyGraph(
                nodes=nodes,
                edges=edges,
                center_id=center_id,
                depth=depth if not full else 0,
                truncated=False,
                total_object_count=total_object_count,
                total_relation_count=total_relation_count,
            )

        # 邻接表：无向展开（源/目标均可作为邻居）
        adjacency: dict[str, set[str]] = {oid: set() for oid in obj_by_id}
        for rel in relations:
            if rel.source_object_type_id in adjacency and rel.target_object_type_id in adjacency:
                adjacency[rel.source_object_type_id].add(rel.target_object_type_id)
                adjacency[rel.target_object_type_id].add(rel.source_object_type_id)

        seed = center_id if center_id in obj_by_id else None
        if seed is None and obj_by_id:
            # 默认选度数最高的对象作为种子，避免冷启动空白图
            seed = max(adjacency, key=lambda oid: len(adjacency[oid]))

        selected: set[str] = set()
        if seed:
            frontier = {seed}
            selected.add(seed)
            for _ in range(max(0, depth)):
                nxt: set[str] = set()
                for nid in frontier:
                    for neighbor in adjacency.get(nid, ()):
                        if neighbor not in selected:
                            nxt.add(neighbor)
                # 超出上限时按度数优先截断
                if len(selected) + len(nxt) > max_nodes:
                    remaining = max_nodes - len(selected)
                    ranked = sorted(nxt, key=lambda oid: len(adjacency.get(oid, ())), reverse=True)
                    selected.update(ranked[:remaining])
                    break
                selected.update(nxt)
                frontier = nxt
                if not frontier:
                    break

        nodes = [
            GraphNode(
                id=obj.id, label=obj.name, display_name=obj.display_name, status=obj.status
            )
            for oid, obj in obj_by_id.items()
            if oid in selected
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
            if rel.source_object_type_id in selected and rel.target_object_type_id in selected
        ]
        return OntologyGraph(
            nodes=nodes,
            edges=edges,
            center_id=seed,
            depth=max(0, depth),
            truncated=len(selected) < total_object_count,
            total_object_count=total_object_count,
            total_relation_count=total_relation_count,
        )

    def list_versions(self, db: Session, entity_id: str) -> list[VersionRecordOut]:
        records = (
            db.query(VersionRecord)
            .filter(VersionRecord.entity_id == entity_id)
            .order_by(VersionRecord.version.desc())
            .all()
        )
        result: list[VersionRecordOut] = []
        for r in records:
            item = VersionRecordOut.model_validate(r)
            item.has_diff = bool(getattr(r, "diff_json", None))
            item.has_snapshot = bool(getattr(r, "snapshot_json", None))
            result.append(item)
        return result

    def get_version_diff(
        self, db: Session, ontology_id: str, version: int
    ) -> "VersionDiffOut | None":
        from app.schemas.ontology import VersionDiffOut, VersionDiffSection
        from app.services.version_diff import parse_diff_json

        record = (
            db.query(VersionRecord)
            .filter(
                VersionRecord.entity_type == "ontology",
                VersionRecord.entity_id == ontology_id,
                VersionRecord.version == version,
            )
            .first()
        )
        if not record:
            return None
        raw = parse_diff_json(getattr(record, "diff_json", None)) or {}
        prev = (
            db.query(VersionRecord.version)
            .filter(
                VersionRecord.entity_type == "ontology",
                VersionRecord.entity_id == ontology_id,
                VersionRecord.version < version,
            )
            .order_by(VersionRecord.version.desc())
            .first()
        )

        def _section(key: str) -> VersionDiffSection:
            section = raw.get(key) or {}
            return VersionDiffSection(
                added=list(section.get("added") or []),
                removed=list(section.get("removed") or []),
                modified=list(section.get("modified") or []),
            )

        return VersionDiffOut(
            ontology_id=ontology_id,
            version=version,
            previous_version=prev[0] if prev else None,
            diff_summary=record.diff_summary,
            operator=record.operator,
            created_at=record.created_at,
            object_types=_section("object_types"),
            properties=_section("properties"),
            relation_types=_section("relation_types"),
            business_logics=_section("business_logics"),
        )

    def get_version_snapshot(
        self, db: Session, ontology_id: str, version: int
    ) -> "VersionSnapshotOut | None":
        import json

        from app.schemas.ontology import VersionSnapshotOut

        record = (
            db.query(VersionRecord)
            .filter(
                VersionRecord.entity_type == "ontology",
                VersionRecord.entity_id == ontology_id,
                VersionRecord.version == version,
            )
            .first()
        )
        if not record:
            return None
        snapshot: dict = {}
        raw = getattr(record, "snapshot_json", None)
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    snapshot = parsed
            except (TypeError, json.JSONDecodeError):
                snapshot = {}

        def _values(key: str) -> list[dict]:
            mapping = snapshot.get(key) or {}
            if isinstance(mapping, dict):
                return list(mapping.values())
            if isinstance(mapping, list):
                return mapping
            return []

        return VersionSnapshotOut(
            ontology_id=ontology_id,
            version=version,
            diff_summary=record.diff_summary,
            created_at=record.created_at,
            object_types=_values("object_types"),
            properties=_values("properties"),
            relation_types=_values("relation_types"),
            business_logics=_values("business_logics"),
        )

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
        domain: tuple[str | None, str | None] | None = None,
    ) -> ObjectTypeSummary:
        if stats is None:
            stats = self._bulk_object_stats(db, [obj.id]).get(obj.id, (0, 0, 0))
        property_count, relation_count, bound_logic_count = stats
        logic_count = bound_logic_count
        if domain is None:
            domain_id, domain_name = self._resolve_domain_context(db, obj.ontology_id)
        else:
            domain_id, domain_name = domain
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
            domain_context_id=domain_id,
            domain_name=domain_name,
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
