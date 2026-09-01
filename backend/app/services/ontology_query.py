import json
import math
from collections import Counter
from dataclasses import dataclass

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.models import (
    BusinessLogic,
    BusinessLogicObjectBinding,
    BusinessLogicPropertyBinding,
    DomainContext,
    DraftEvidence,
    EntityStatus,
    ObjectType,
    Ontology,
    OntologySegment,
    OntologyStatus,
    Property,
    RelationType,
    VersionRecord,
)
from app.schemas import (
    ClusterDetail,
    ClusterNode,
    GraphCluster,
    GraphEdge,
    GraphNode,
    GraphPoint,
    GroupedGraphEdge,
    HubNode,
    ObjectLandingOut,
    ObjectTypeSummary,
    OntologyGraph,
    OntologyGroupedGraph,
    OntologySummary,
    PageResult,
    RelationObjectRef,
    RelationGroupOut,
    RelationTypeDetail,
    RelationTypeOut,
    VersionRecordOut,
)
from app.services.object_landing import ObjectLanding, bulk_object_landings
from app.services.community_detection import (
    compute_graph_layout,
    identify_hub_nodes,
    label_propagation_clusters,
    name_cluster,
    split_dominant_clusters,
)
from app.services.relation_structure import infer_relation_structure_type

# 图谱局部展开默认节点上限（避免一次渲染全图）
_DEFAULT_GRAPH_MAX_NODES = 80

# 单个聚类内展示的节点上限（超出则截断，前端显示 "+N more"）
_DEFAULT_CLUSTER_MAX_NODES = 50

# 语义缩放展开时，单个版块最多平铺的成员卡片数（与前端 OVERVIEW_MEMBER_CAP 对应）。
# 用它估算版块展开后的占地半径，供布局做尺寸感知的去重叠。
_LOD_MEMBER_CAP = 24
# 前端概览的像素常量镜像：成员卡片格子宽/高、坐标单位对应的像素间距（OVERVIEW_SPACING）。
_OVERVIEW_CELL_W = 196.0
_OVERVIEW_CELL_H = 96.0
_OVERVIEW_SPACING = 340.0
_OVERVIEW_HUB_RADIUS_UNITS = 0.3


def _cluster_layout_radius(node_count: int) -> float:
    """版块展开成 N×N 成员网格后，外接圆在布局单位下的半径（1 单位 ≈ 前端 OVERVIEW_SPACING 像素）。"""
    shown = max(1, min(node_count, _LOD_MEMBER_CAP))
    cols = math.ceil(math.sqrt(shown))
    rows = math.ceil(shown / cols)
    grid_px = max(cols * _OVERVIEW_CELL_W, rows * _OVERVIEW_CELL_H)
    return grid_px / 2.0 / _OVERVIEW_SPACING

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

@dataclass
class _ClusterPartition:
    """一次聚类划分的中间结果，供 grouped-graph 与单簇下钻共用。

    关键：同一份数据、同一 max_cluster_nodes → 得到完全相同的簇 id 与成员划分
    （聚类确定性 + 度数降序命名），这样前端拿概览图里的 cluster_id 回来下钻矩阵不会错位。
    """

    obj_by_id: dict[str, "ObjectType"]
    adjacency: dict[str, set[str]]
    cluster_order: list[str]                # 展示顺序的簇 id
    cluster_members: dict[str, list[str]]   # 簇 id -> 按度降序的全量成员 id（不截断）
    cluster_names: dict[str, str]           # 簇 id -> 名称（已去重）
    hub_ids_ordered: list[str]              # 按度降序的枢纽 id
    cluster_of: dict[str, str]              # 对象/枢纽 id -> 宏观节点 id
    isolated_ids: list[str]


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
        def _finalize(scoped):
            # 已发布浏览/下游只暴露"已发布"实体本身：配合"数据域发布仅发布业务对象"，
            # 未被发布晋级的关系、业务逻辑、非业务对象因此不会出现在已发布视图。
            if published_only and hasattr(ontology_model, "status"):
                return scoped.filter(
                    ontology_model.status == EntityStatus.PUBLISHED.value
                )
            return scoped

        if ontology_id:
            if published_only:
                ontology = db.get(Ontology, ontology_id)
                if not ontology or ontology.status != OntologyStatus.PUBLISHED.value:
                    return query.filter(False)
            return _finalize(query.filter(ontology_model.ontology_id == ontology_id))

        if domain_context_id:
            ontologies = db.query(Ontology).filter(
                Ontology.domain_context_id == domain_context_id
            )
            if published_only:
                ontologies = ontologies.filter(Ontology.status == OntologyStatus.PUBLISHED.value)
            ontology_ids = [o.id for o in ontologies.all()]
            if not ontology_ids:
                return query.filter(False)
            return _finalize(query.filter(ontology_model.ontology_id.in_(ontology_ids)))

        if published_only:
            ontology_ids = self._published_ontology_ids(db)
            if not ontology_ids:
                return query.filter(False)
            return _finalize(query.filter(ontology_model.ontology_id.in_(ontology_ids)))

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
        self, db: Session, ontology_ids: list[str], *, published_only: bool = False
    ) -> dict[str, tuple[int, int, int]]:
        """批量返回 ontology_id -> (object_type_count, relation_type_count, business_logic_count)。

        published_only=True 时只计已发布实体；配合"仅发布业务对象"，对象计数即
        已发布业务对象数，关系/业务逻辑计数为 0（它们不随本体发布）。
        """
        if not ontology_ids:
            return {}
        object_q = db.query(ObjectType.ontology_id, func.count(ObjectType.id)).filter(
            ObjectType.ontology_id.in_(ontology_ids)
        )
        relation_q = db.query(RelationType.ontology_id, func.count(RelationType.id)).filter(
            RelationType.ontology_id.in_(ontology_ids)
        )
        logic_q = db.query(BusinessLogic.ontology_id, func.count(BusinessLogic.id)).filter(
            BusinessLogic.ontology_id.in_(ontology_ids)
        )
        if published_only:
            object_q = object_q.filter(ObjectType.status == EntityStatus.PUBLISHED.value)
            relation_q = relation_q.filter(RelationType.status == EntityStatus.PUBLISHED.value)
            logic_q = logic_q.filter(BusinessLogic.status == EntityStatus.PUBLISHED.value)
        object_rows = object_q.group_by(ObjectType.ontology_id).all()
        relation_rows = relation_q.group_by(RelationType.ontology_id).all()
        logic_rows = logic_q.group_by(BusinessLogic.ontology_id).all()
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
        role_in: list[str] | None = None,
        needs_review: bool | None = None,
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
        # 组合筛选（AND）：role_in=对象角色多选；needs_review=仅看待复核。
        # 复核状态是独立列（此前寄生在 role_reason 的 [待复核] 前缀里，靠全表 LIKE 扫）。
        if role_in:
            query = query.filter(ObjectType.table_role.in_(role_in))
        if needs_review is not None:
            query = query.filter(ObjectType.needs_review.is_(bool(needs_review)))
        normalized_q = (q or "").strip()
        # Agent/搜索框约定 ``*`` 表示不限定关键词，不能把它当成字面星号去做 ILIKE。
        if normalized_q and normalized_q != "*":
            like = f"%{normalized_q}%"
            query = query.filter(
                (ObjectType.name.ilike(like))
                | (ObjectType.display_name.ilike(like))
                | (ObjectType.description.ilike(like))
                # 用户经常拿物理表名（如 ``tabCode List``）找业务对象；该信息只在
                # DataHub URN/source_ref 中，遗漏它会让已绑定对象看起来像不存在。
                | (ObjectType.source_ref.ilike(like))
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
        landings = bulk_object_landings(db, [o.id for o in objects])
        items = [
            self._to_object_summary(
                db,
                obj,
                stats=stats.get(obj.id),
                domain=domain_map.get(obj.ontology_id),
                landing=landings.get(obj.id),
                landing_loaded=True,
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

    def _object_referenced_logic_map(
        self, db: Session, object_ids: list[str]
    ) -> dict[str, set[str]]:
        """object_type_id -> {business_logic_id}，基于口径表达式的 refs 引用。

        仅扫描这些对象所属本体下的业务逻辑，避免全表扫描。与
        ``logic_query.OntologyQueryService`` 的同名方法同口径（都走
        ``_logic_referenced_ids``）——此处补齐是因为 ``_bulk_object_stats``
        位在本类，而拆分后该方法只落到了另一个同名类上。
        """
        if not object_ids:
            return {}
        rows = (
            db.query(ObjectType.id, ObjectType.ontology_id)
            .filter(ObjectType.id.in_(object_ids))
            .all()
        )
        ontology_ids = {ont_id for _, ont_id in rows}
        result: dict[str, set[str]] = {oid: set() for oid in object_ids}
        if not ontology_ids:
            return result
        logics = (
            db.query(BusinessLogic)
            .filter(BusinessLogic.ontology_id.in_(list(ontology_ids)))
            .all()
        )
        for logic in logics:
            ref_obj_ids, _ = _logic_referenced_ids(logic)
            for oid in ref_obj_ids:
                if oid in result:
                    result[oid].add(logic.id)
        return result

    def list_relation_types(
        self,
        db: Session,
        ontology_id: str | None = None,
        domain_context_id: str | None = None,
        published_only: bool = False,
        *,
        q: str | None = None,
        display_name: str | None = None,
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
        if display_name is not None:
            query = query.filter(RelationType.display_name == display_name)
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

    def list_relation_groups(
        self,
        db: Session,
        ontology_id: str | None = None,
        domain_context_id: str | None = None,
        published_only: bool = False,
        *,
        q: str | None = None,
    ) -> list[RelationGroupOut]:
        """按 display_name 折叠关系为分组（关系去重列表用）。

        scope / q 过滤与 ``list_relation_types`` 完全一致；聚合在 Python 内完成
        （SQLite 无 array_agg，且 scope 行数有限），并对基数/结构类型套用与
        ``_to_relation_out`` 相同的归一化，保证列表与详情三元组口径一致。
        """
        query = db.query(RelationType)
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

        groups: dict[str, dict] = {}
        for rel in query.all():
            key = rel.display_name or ""
            g = groups.get(key)
            if g is None:
                g = {
                    "count": 0,
                    "structure_types": set(),
                    "cardinalities": set(),
                    "statuses": set(),
                    "conf_min": None,
                    "conf_max": None,
                    "descriptions": Counter(),
                }
                groups[key] = g
            g["count"] += 1
            structure = rel.structure_type or infer_relation_structure_type(
                rel.description, rel.source_evidence
            )
            if structure:
                g["structure_types"].add(structure)
            cardinality = _normalize_cardinality(rel.cardinality)
            if cardinality:
                g["cardinalities"].add(cardinality)
            if rel.status:
                g["statuses"].add(rel.status)
            if rel.source_confidence is not None:
                c = rel.source_confidence
                g["conf_min"] = c if g["conf_min"] is None else min(g["conf_min"], c)
                g["conf_max"] = c if g["conf_max"] is None else max(g["conf_max"], c)
            if rel.description:
                g["descriptions"][rel.description] += 1

        result = [
            RelationGroupOut(
                display_name=name,
                count=g["count"],
                description=(g["descriptions"].most_common(1)[0][0] if g["descriptions"] else None),
                structure_types=sorted(g["structure_types"]),
                cardinalities=sorted(g["cardinalities"]),
                confidence_min=g["conf_min"],
                confidence_max=g["conf_max"],
                statuses=sorted(g["statuses"]),
            )
            for name, g in groups.items()
        ]
        result.sort(key=lambda r: r.count, reverse=True)
        return result


    def get_relation_type(
        self, db: Session, relation_type_id: str, *, published_only: bool = False
    ) -> RelationTypeDetail | None:
        rel = db.get(RelationType, relation_type_id)
        if not rel:
            return None
        # 已发布浏览：未发布的关系视为不存在，与 Data Agent 接地集一致。
        if published_only and rel.status != EntityStatus.PUBLISHED.value:
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
        published_only: bool = False,
    ) -> OntologyGraph:
        obj_q = db.query(ObjectType).filter(ObjectType.ontology_id == ontology_id)
        rel_q = db.query(RelationType).filter(RelationType.ontology_id == ontology_id)
        if published_only:
            # 与已发布列表/详情/Data Agent 一致：图谱只展现已发布实体。
            _pub = EntityStatus.PUBLISHED.value
            obj_q = obj_q.filter(ObjectType.status == _pub)
            rel_q = rel_q.filter(RelationType.status == _pub)
        objects = obj_q.all()
        relations = rel_q.all()
        total_object_count = len(objects)
        total_relation_count = len(relations)
        obj_by_id = {obj.id: obj for obj in objects}

        if full or total_object_count <= max_nodes:
            nodes = [
                GraphNode(
                    id=obj.id,
                    label=obj.name,
                    display_name=obj.display_name,
                    status=obj.status,
                    table_role=obj.table_role,
                    needs_review=bool(obj.needs_review),
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
                id=obj.id,
                label=obj.name,
                display_name=obj.display_name,
                status=obj.status,
                table_role=obj.table_role,
                needs_review=bool(obj.needs_review),
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

    def _compute_cluster_partition(
        self,
        objects: list[ObjectType],
        relations: list[RelationType],
        *,
        max_cluster_nodes: int,
    ) -> _ClusterPartition:
        """把 ObjectType 划分为业务子域 + 枢纽骨架。grouped-graph 与单簇下钻共用此结果。"""
        obj_by_id = {obj.id: obj for obj in objects}

        # 无向邻接（忽略自环），用于聚类与度数计算
        adjacency: dict[str, set[str]] = {oid: set() for oid in obj_by_id}
        for rel in relations:
            s, t = rel.source_object_type_id, rel.target_object_type_id
            if s in adjacency and t in adjacency and s != t:
                adjacency[s].add(t)
                adjacency[t].add(s)

        isolated_ids = [oid for oid in obj_by_id if not adjacency.get(oid)]
        clustered_ids = [oid for oid in obj_by_id if adjacency.get(oid)]

        # 摘除枢纽节点（公共维度表，几乎处处被引用）后再聚类，避免它们把大半张图
        # 传递闭包般粘成一个巨簇；枢纽节点摘除后作为独立单节点簇展示。
        max_hub_count = min(40, max(5, len(clustered_ids) // 20))
        hub_ids = identify_hub_nodes(
            {oid: adjacency[oid] for oid in clustered_ids}, max_hub_count
        )
        non_hub_ids = [oid for oid in clustered_ids if oid not in hub_ids]
        reduced_adjacency = {
            oid: {n for n in adjacency[oid] if n not in hub_ids} for oid in non_hub_ids
        }

        raw_clusters = (
            label_propagation_clusters(non_hub_ids, reduced_adjacency) if non_hub_ids else []
        )
        raw_clusters = split_dominant_clusters(
            raw_clusters, reduced_adjacency, max_cluster_nodes, len(non_hub_ids)
        )
        # 摘除枢纽后仍然落单的节点（只挂在枢纽上，没有同伴业务对象一起聚类）
        # 归入孤立节点展示，避免大量单节点簇淹没真正有业务含义的聚类。
        stray_singletons = [c for c in raw_clusters if len(c) == 1]
        raw_clusters = [c for c in raw_clusters if len(c) > 1]
        isolated_ids.extend(next(iter(c)) for c in stray_singletons)

        # 度数降序排列聚类，保证结果确定且大聚类优先展示
        raw_clusters.sort(key=lambda c: (-len(c), min(c)))

        cluster_of: dict[str, str] = {}
        cluster_order: list[str] = []
        cluster_members: dict[str, list[str]] = {}
        cluster_names: dict[str, str] = {}
        used_names: dict[str, int] = {}
        for idx, member_ids in enumerate(raw_clusters):
            cluster_id = f"cluster-{idx}"
            for oid in member_ids:
                cluster_of[oid] = cluster_id

            name = name_cluster(member_ids, obj_by_id, adjacency)
            if name in used_names:
                used_names[name] += 1
                name = f"{name} ({used_names[name]})"
            else:
                used_names[name] = 0

            ranked_members = sorted(
                member_ids,
                key=lambda oid: len(adjacency.get(oid, ())),
                reverse=True,
            )
            cluster_order.append(cluster_id)
            cluster_members[cluster_id] = ranked_members
            cluster_names[cluster_id] = name

        # 枢纽以自身对象 id 作为宏观节点：既让跨版块关系聚合到枢纽上，也让它作为主干骨架独立展示。
        hub_ids_ordered = sorted(hub_ids, key=lambda h: (-len(adjacency.get(h, ())), h))
        for hub_id in hub_ids_ordered:
            cluster_of[hub_id] = hub_id

        return _ClusterPartition(
            obj_by_id=obj_by_id,
            adjacency=adjacency,
            cluster_order=cluster_order,
            cluster_members=cluster_members,
            cluster_names=cluster_names,
            hub_ids_ordered=hub_ids_ordered,
            cluster_of=cluster_of,
            isolated_ids=isolated_ids,
        )

    def get_ontology_grouped_graph(
        self,
        db: Session,
        ontology_id: str,
        *,
        published_only: bool = False,
        max_cluster_nodes: int = _DEFAULT_CLUSTER_MAX_NODES,
    ) -> OntologyGroupedGraph:
        """域层级概览图：读取落库的板块数据，聚合跨簇关系。

        Args:
            published_only: True = 只读已发布状态的对象/关系/板块，False = 读草稿态
        """
        # 构建对象/关系查询
        obj_query = db.query(ObjectType).filter(ObjectType.ontology_id == ontology_id)
        rel_query = db.query(RelationType).filter(RelationType.ontology_id == ontology_id)

        if published_only:
            obj_query = obj_query.filter(ObjectType.status == EntityStatus.PUBLISHED)
            rel_query = rel_query.filter(RelationType.status == EntityStatus.PUBLISHED)

        objects = obj_query.all()
        relations = rel_query.all()
        total_object_count = len(objects)
        total_relation_count = len(relations)

        # 读取落库的板块数据
        segments = db.query(OntologySegment).filter(
            OntologySegment.ontology_id == ontology_id
        ).all()

        # 如果没有落库的板块，回退到旧的聚类算法
        if not segments:
            part = self._compute_cluster_partition(
                objects, relations, max_cluster_nodes=max_cluster_nodes
            )
            obj_by_id = part.obj_by_id
            adjacency = part.adjacency

            # 使用旧的聚类结果构建返回数据
            def to_cluster_node(oid: str) -> ClusterNode:
                obj = obj_by_id[oid]
                return ClusterNode(
                    id=obj.id, label=obj.name, display_name=obj.display_name, status=obj.status
                )

            clusters: list[GraphCluster] = []
            for cid in part.cluster_order:
                members = part.cluster_members[cid]
                truncated = len(members) > max_cluster_nodes
                shown_members = members[:max_cluster_nodes]
                clusters.append(
                    GraphCluster(
                        id=cid,
                        name=part.cluster_names[cid],
                        nodes=[to_cluster_node(oid) for oid in shown_members],
                        node_count=len(members),
                        truncated=truncated,
                    )
                )

            hub_nodes: list[HubNode] = []
            for hub_id in part.hub_ids_ordered:
                obj = obj_by_id[hub_id]
                hub_nodes.append(
                    HubNode(
                        id=hub_id,
                        label=obj.name,
                        display_name=obj.display_name,
                        status=obj.status,
                        degree=len(adjacency.get(hub_id, ())),
                    )
                )
        else:
            # 使用落库的板块数据
            obj_by_id = {obj.id: obj for obj in objects}

            # 构建邻接表
            adjacency: dict[str, set[str]] = {}
            for rel in relations:
                adjacency.setdefault(rel.source_object_type_id, set()).add(rel.target_object_type_id)
                adjacency.setdefault(rel.target_object_type_id, set()).add(rel.source_object_type_id)

            def to_cluster_node(obj: ObjectType) -> ClusterNode:
                return ClusterNode(
                    id=obj.id, label=obj.name, display_name=obj.display_name, status=obj.status
                )

            # 构建板块节点
            clusters: list[GraphCluster] = []
            for seg in segments:
                # 通过 relationship 获取成员对象
                member_objects = seg.members  # 这是 ObjectType 列表

                # 按度数排序
                ranked_members = sorted(
                    member_objects,
                    key=lambda obj: len(adjacency.get(obj.id, set())),
                    reverse=True,
                )

                truncated = len(ranked_members) > max_cluster_nodes
                shown_members = ranked_members[:max_cluster_nodes]

                clusters.append(
                    GraphCluster(
                        id=seg.id,
                        name=seg.display_name,
                        nodes=[to_cluster_node(obj) for obj in shown_members],
                        node_count=len(member_objects),
                        truncated=truncated,
                    )
                )

            # 识别枢纽节点（is_hub=True）
            hub_objects = [obj for obj in objects if getattr(obj, 'is_hub', False)]
            hub_nodes: list[HubNode] = []
            for obj in hub_objects:
                hub_nodes.append(
                    HubNode(
                        id=obj.id,
                        label=obj.name,
                        display_name=obj.display_name,
                        status=obj.status,
                        degree=len(adjacency.get(obj.id, set())),
                    )
                )

            # 识别孤立节点（未分配到板块且非枢纽）
            hub_ids = {h.id for h in hub_nodes}
            part_isolated_ids = [
                obj.id for obj in objects
                if obj.segment_id is None and obj.id not in hub_ids
            ]

        # 跨版块关系聚合（同一宏观节点内部的关系不展示，只关心宏观关系）
        edge_agg: dict[tuple[str, str], GroupedGraphEdge] = {}

        # 构建对象到板块/枢纽的映射
        cluster_of: dict[str, str] = {}
        if segments:
            for seg in segments:
                for obj in seg.members:
                    cluster_of[obj.id] = seg.id
            for hub in hub_nodes:
                cluster_of[hub.id] = hub.id
        else:
            cluster_of = part.cluster_of

        for rel in relations:
            s_cluster = cluster_of.get(rel.source_object_type_id)
            t_cluster = cluster_of.get(rel.target_object_type_id)
            if not s_cluster or not t_cluster or s_cluster == t_cluster:
                continue
            key = tuple(sorted((s_cluster, t_cluster)))
            existing = edge_agg.get(key)
            if existing:
                existing.weight += 1
                existing.relation_ids.append(rel.id)
            else:
                edge_agg[key] = GroupedGraphEdge(
                    id=f"cluster-edge-{key[0]}-{key[1]}",
                    source_cluster_id=s_cluster,
                    target_cluster_id=t_cluster,
                    weight=1,
                    relation_ids=[rel.id],
                )

        # 稳定坐标：对"聚类 + 枢纽"构成的宏观图跑一次确定性力导向布局
        layout_nodes = [c.id for c in clusters] + [h.id for h in hub_nodes]
        layout_edges = [
            (e.source_cluster_id, e.target_cluster_id, float(e.weight))
            for e in edge_agg.values()
        ]
        layout_sizes = {c.id: _cluster_layout_radius(c.node_count) for c in clusters}
        layout_sizes.update({h.id: _OVERVIEW_HUB_RADIUS_UNITS for h in hub_nodes})
        positions = compute_graph_layout(layout_nodes, layout_edges, sizes=layout_sizes)
        for cluster in clusters:
            pos = positions.get(cluster.id)
            if pos:
                cluster.layout = GraphPoint(x=pos[0], y=pos[1])
        for hub in hub_nodes:
            pos = positions.get(hub.id)
            if pos:
                hub.layout = GraphPoint(x=pos[0], y=pos[1])

        # 孤立节点列表
        isolated_nodes = []
        if segments:
            def to_isolated_node(oid: str) -> ClusterNode | None:
                obj = obj_by_id.get(oid)
                if not obj:
                    return None
                return ClusterNode(
                    id=obj.id, label=obj.name, display_name=obj.display_name, status=obj.status
                )
            isolated_nodes = [to_isolated_node(oid) for oid in part_isolated_ids]
            isolated_nodes = [n for n in isolated_nodes if n is not None]
        else:
            def to_cluster_node_old(oid: str) -> ClusterNode:
                obj = obj_by_id[oid]
                return ClusterNode(
                    id=obj.id, label=obj.name, display_name=obj.display_name, status=obj.status
                )
            isolated_nodes = [to_cluster_node_old(oid) for oid in part.isolated_ids]

        return OntologyGroupedGraph(
            clusters=clusters,
            hub_nodes=hub_nodes,
            edges=list(edge_agg.values()),
            isolated_nodes=isolated_nodes,
            total_object_count=total_object_count,
            total_relation_count=total_relation_count,
        )

    def get_ontology_cluster_detail(
        self,
        db: Session,
        ontology_id: str,
        cluster_id: str,
        *,
        max_cluster_nodes: int = _DEFAULT_CLUSTER_MAX_NODES,
    ) -> ClusterDetail | None:
        """单个聚类的下钻详情：全量成员 + 簇内关系边（供邻接矩阵）。

        cluster_id 必须来自同一 max_cluster_nodes 下的 grouped-graph（默认 50）——聚类确定性
        保证同一份数据得到同样的簇划分。找不到该簇返回 None（由路由转 404）。
        """
        objects = db.query(ObjectType).filter(ObjectType.ontology_id == ontology_id).all()
        relations = db.query(RelationType).filter(RelationType.ontology_id == ontology_id).all()

        part = self._compute_cluster_partition(
            objects, relations, max_cluster_nodes=max_cluster_nodes
        )
        members = part.cluster_members.get(cluster_id)
        if members is None:
            return None

        member_set = set(members)
        obj_by_id = part.obj_by_id
        nodes = [
            GraphNode(
                id=obj_by_id[oid].id,
                label=obj_by_id[oid].name,
                display_name=obj_by_id[oid].display_name,
                status=obj_by_id[oid].status,
                table_role=obj_by_id[oid].table_role,
                needs_review=bool(obj_by_id[oid].needs_review),
            )
            for oid in members
        ]
        edges = [
            GraphEdge(
                id=rel.id,
                source=rel.source_object_type_id,
                target=rel.target_object_type_id,
                label=rel.display_name,
                cardinality=_normalize_cardinality(rel.cardinality),
                relation_id=rel.id,
                structure_type=rel.structure_type,
            )
            for rel in relations
            if rel.source_object_type_id in member_set
            and rel.target_object_type_id in member_set
        ]
        return ClusterDetail(
            id=cluster_id,
            name=part.cluster_names[cluster_id],
            node_count=len(members),
            nodes=nodes,
            edges=edges,
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
        landing: ObjectLanding | None = None,
        landing_loaded: bool = False,
    ) -> ObjectTypeSummary:
        if stats is None:
            stats = self._bulk_object_stats(db, [obj.id]).get(obj.id, (0, 0, 0))
        property_count, relation_count, bound_logic_count = stats
        logic_count = bound_logic_count
        if domain is None:
            domain_id, domain_name = self._resolve_domain_context(db, obj.ontology_id)
        else:
            domain_id, domain_name = domain
        # 落点的「查过了但没有」与「压根没查」必须分得开：前者是真·未落地，后者是漏传。
        # 比照 stats 的自愈写法——列表页批量传入，单对象调用就地补查，故任何调用点
        # 都不会静默退化成「全部未落地」（``source_provenance`` 曾栽在这上面）。
        if landing is None and not landing_loaded:
            landing = bulk_object_landings(db, [obj.id]).get(obj.id)

        # 查询板块信息
        segment_name = None
        if obj.segment_id:
            segment = db.get(OntologySegment, obj.segment_id)
            if segment:
                segment_name = segment.display_name

        return ObjectTypeSummary(
            id=obj.id,
            name=obj.name,
            display_name=obj.display_name,
            description=obj.description,
            source_ref=obj.source_ref,
            # 派生属性要**显式**带上：本读模型是按关键字构造的，``from_attributes`` 只在
            # 从 ORM 对象校验时才生效。漏掉这一项 → 恒取默认值 "none" → 前端把每个对象
            # 都判成「无源表，不可同步」，同步向导里 483 个对象全部置灰、一个都选不了。
            source_provenance=obj.source_provenance,
            status=obj.status,
            property_count=property_count,
            relation_count=relation_count,
            business_logic_count=logic_count,
            bound_logic_count=bound_logic_count,
            source_confidence=obj.source_confidence,
            table_role=obj.table_role,
            role_confidence=obj.role_confidence,
            role_reason=obj.role_reason,
            segment_id=obj.segment_id,
            segment_name=segment_name,
            domain_context_id=domain_id,
            domain_name=domain_name,
            landing=(
                ObjectLandingOut.model_validate(landing) if landing is not None else None
            ),
            updated_at=obj.updated_at,
            origin=obj.origin or "machine",
            upstream_removed=bool(obj.upstream_removed),
            has_conflict=obj.has_conflict,
            pinned_fields=obj.pinned_fields,
            conflicts=obj.conflicts,
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
            origin=rel.origin or "machine",
            upstream_removed=bool(rel.upstream_removed),
            has_conflict=rel.has_conflict,
            pinned_fields=rel.pinned_fields,
            conflicts=rel.conflicts,
        )

    def _resolve_object_datahub(
        self, db: Session, obj: ObjectType
    ) -> tuple[str | None, str | None]:
        from app.connectors.datahub import DataHubConnector
        from app.services.settings_service import SettingsService

        datahub = DataHubConnector(SettingsService().get_datahub_runtime(db))
        if obj.source_ref:
            if not _refers_to_dataset(obj.source_ref):
                # 人工建模与派生对象在 DataHub 里没有数据集。get_dataset_url 会把
                # `derived:<本体>:<标识>` 当表名拼成一个 hive URN，前端于是显示一个
                # 点开必然空白的「在 DataHub 中查看表详情」。引用照给，链接不给。
                return obj.source_ref, None
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

    def list_segments(
        self,
        db: Session,
        ontology_id: str,
        published_only: bool = False,
        q: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ):
        """列出本体的业务板块。"""
        from app.models import OntologySegment
        from app.schemas import PageResult, SegmentSummary

        query_obj = db.query(OntologySegment).filter(
            OntologySegment.ontology_id == ontology_id
        )

        # 搜索过滤
        if q:
            q_lower = q.lower()
            query_obj = query_obj.filter(
                (OntologySegment.name.ilike(f"%{q}%"))
                | (OntologySegment.display_name.ilike(f"%{q}%"))
                | (OntologySegment.description.ilike(f"%{q}%"))
            )

        # 软删除过滤
        if not published_only:
            query_obj = query_obj.filter(OntologySegment.deleted_by_user == False)

        # 总数
        total = query_obj.count()

        # 分页
        if limit is not None:
            query_obj = query_obj.offset(offset).limit(limit)

        segments = query_obj.order_by(OntologySegment.display_name).all()

        # 构建 SegmentSummary
        items = []
        for seg in segments:
            items.append(
                SegmentSummary(
                    id=seg.id,
                    name=seg.name,
                    display_name=seg.display_name,
                    description=seg.description,
                    member_count=seg.member_count,
                    ontology_id=seg.ontology_id,
                    needs_review=seg.needs_review,
                    updated_at=seg.updated_at,
                    origin=seg.origin or "machine",
                    upstream_removed=bool(seg.upstream_removed),
                    has_conflict=bool(seg.conflict_json),
                    pinned_fields=_loads(seg.overridden_fields, []),
                    conflicts=_loads_json(seg.conflict_json) or {},
                )
            )

        return PageResult(items=items, total=total, limit=limit, offset=offset)

    def get_segment_detail(self, db: Session, segment_id: str):
        """获取板块详情（包含成员列表）。

        对于稠密板块（成员 > 40），返回边数据以支持矩阵视图。
        """
        from app.models import OntologySegment, ObjectType, RelationType
        from app.schemas import SegmentDetail, GraphEdge

        segment = db.query(OntologySegment).filter(OntologySegment.id == segment_id).first()
        if not segment:
            return None

        # 获取成员对象
        members = (
            db.query(ObjectType)
            .filter(
                ObjectType.segment_id == segment_id,
                ObjectType.deleted_by_user == False,
            )
            .all()
        )

        # 转换为 ObjectTypeSummary
        member_summaries = [self._to_object_summary(db, obj) for obj in members]

        # 获取板块内关系
        member_ids = {obj.id for obj in members}
        internal_relations = (
            db.query(RelationType)
            .filter(
                RelationType.ontology_id == segment.ontology_id,
                RelationType.source_object_type_id.in_(member_ids),
                RelationType.target_object_type_id.in_(member_ids),
                RelationType.deleted_by_user == False,
            )
            .all()
        )

        internal_relation_count = len(internal_relations)

        # 对于稠密板块（> 40 成员），返回边数据用于矩阵视图
        edges = None
        if len(members) > 40:
            edges = [
                GraphEdge(
                    id=rel.id,
                    source=rel.source_object_type_id,
                    target=rel.target_object_type_id,
                    label=rel.display_name,
                    cardinality=_normalize_cardinality(rel.cardinality),
                    relation_id=rel.id,
                    structure_type=rel.structure_type,
                )
                for rel in internal_relations
            ]

        return SegmentDetail(
            id=segment.id,
            name=segment.name,
            display_name=segment.display_name,
            description=segment.description,
            member_count=segment.member_count,
            ontology_id=segment.ontology_id,
            needs_review=segment.needs_review,
            updated_at=segment.updated_at,
            origin=segment.origin or "machine",
            upstream_removed=bool(segment.upstream_removed),
            has_conflict=bool(segment.conflict_json),
            pinned_fields=_loads(segment.overridden_fields, []),
            conflicts=_loads_json(segment.conflict_json) or {},
            members=member_summaries,
            internal_relation_count=internal_relation_count,
            edges=edges,
        )


def _refers_to_dataset(ref: str | None) -> bool:
    """这个 source_ref 在 DataHub 里是否可能对应一个数据集。

    只排除已知的两种非数据集形态（``manual:`` / ``derived:``），不改历史上那条
    「裸库表名 → 兜底拼 hive URN」的行为——存量对象里确实有靠它出链接的。
    """
    from app.services.source_ref import is_derived_source_ref, is_manual_source_ref

    return bool(ref) and not is_manual_source_ref(ref) and not is_derived_source_ref(ref)
