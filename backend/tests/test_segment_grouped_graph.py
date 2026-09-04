"""测试板块与 grouped-graph 的集成"""
import uuid
import pytest
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import (
    DomainContext,
    ObjectType,
    RelationType,
    Ontology,
    OntologySegment,
    EntityStatus,
    OntologyStatus,
)
from app.services.ontology_query import OntologyQueryService


def _uuid() -> str:
    return str(uuid.uuid4())


def _fresh_ontology(db: Session) -> Ontology:
    domain = DomainContext(datahub_domain_id=f"urn:test:{uuid.uuid4()}", name="测试域")
    db.add(domain)
    db.flush()
    ontology = Ontology(
        domain_context_id=domain.id, status=OntologyStatus.DRAFT.value, generated_by="test"
    )
    db.add(ontology)
    db.flush()
    return ontology


def test_grouped_graph_uses_persisted_segments():
    """测试 grouped-graph 优先使用落库的板块数据"""
    db = SessionLocal()
    try:
        ontology = _fresh_ontology(db)
        seg1_id = _uuid()
        seg2_id = _uuid()

        # 创建测试对象
        obj1 = ObjectType(
            id=_uuid(),
            ontology_id=ontology.id,
            name="customer",
            display_name="客户",
            status=EntityStatus.SUGGESTED.value,
            segment_id=seg1_id,
            is_hub=False,
        )
        obj2 = ObjectType(
            id=_uuid(),
            ontology_id=ontology.id,
            name="order",
            display_name="订单",
            status=EntityStatus.SUGGESTED.value,
            segment_id=seg1_id,
            is_hub=False,
        )
        obj3 = ObjectType(
            id=_uuid(),
            ontology_id=ontology.id,
            name="product",
            display_name="产品",
            status=EntityStatus.SUGGESTED.value,
            segment_id=seg2_id,
            is_hub=False,
        )
        db.add_all([obj1, obj2, obj3])

        # 创建关系
        rel1 = RelationType(
            id=_uuid(),
            ontology_id=ontology.id,
            source_object_type_id=obj1.id,
            target_object_type_id=obj2.id,
            name="places",
            display_name="下单",
            status=EntityStatus.SUGGESTED.value,
        )
        rel2 = RelationType(
            id=_uuid(),
            ontology_id=ontology.id,
            source_object_type_id=obj2.id,
            target_object_type_id=obj3.id,
            name="contains",
            display_name="包含",
            status=EntityStatus.SUGGESTED.value,
        )
        db.add_all([rel1, rel2])

        # 创建板块
        seg1 = OntologySegment(
            id=seg1_id,
            ontology_id=ontology.id,
            name="customer_management",
            display_name="客户管理",
            description="客户与订单",
            member_count=2,
        )
        seg2 = OntologySegment(
            id=seg2_id,
            ontology_id=ontology.id,
            name="product_catalog",
            display_name="产品目录",
            description="产品信息",
            member_count=1,
        )
        db.add_all([seg1, seg2])
        db.commit()

        # 调用 grouped-graph API
        service = OntologyQueryService()
        result = service.get_ontology_grouped_graph(db, ontology.id, published_only=False)

        # 验证使用了落库的板块
        assert len(result.clusters) == 2
        cluster_names = {c.name for c in result.clusters}
        assert "客户管理" in cluster_names
        assert "产品目录" in cluster_names

        # 验证板块成员数
        seg1_cluster = next(c for c in result.clusters if c.name == "客户管理")
        assert seg1_cluster.node_count == 2

        seg2_cluster = next(c for c in result.clusters if c.name == "产品目录")
        assert seg2_cluster.node_count == 1

        # 验证跨板块边
        assert len(result.edges) == 1
        edge = result.edges[0]
        assert edge.weight == 1

    finally:
        db.rollback()
        db.close()


def test_grouped_graph_fallback_without_segments():
    """测试当没有落库板块时，回退到旧的聚类算法"""
    db = SessionLocal()
    try:
        ontology = _fresh_ontology(db)

        # 只创建对象和关系，不创建板块
        obj1 = ObjectType(
            id=_uuid(),
            ontology_id=ontology.id,
            name="customer",
            display_name="客户",
            status=EntityStatus.SUGGESTED.value,
        )
        obj2 = ObjectType(
            id=_uuid(),
            ontology_id=ontology.id,
            name="order",
            display_name="订单",
            status=EntityStatus.SUGGESTED.value,
        )
        db.add_all([obj1, obj2])

        rel1 = RelationType(
            id=_uuid(),
            ontology_id=ontology.id,
            source_object_type_id=obj1.id,
            target_object_type_id=obj2.id,
            name="places",
            display_name="下单",
            status=EntityStatus.SUGGESTED.value,
        )
        db.add(rel1)
        db.commit()

        # 调用 grouped-graph API
        service = OntologyQueryService()
        result = service.get_ontology_grouped_graph(db, ontology.id, published_only=False)

        # 应该有结果（回退到旧算法）
        assert result.total_object_count == 2
        assert result.total_relation_count == 1

    finally:
        db.rollback()
        db.close()


def test_grouped_graph_published_only_filter():
    """测试 published_only 参数正确过滤草稿态数据"""
    db = SessionLocal()
    try:
        ontology = _fresh_ontology(db)
        seg1_id = _uuid()

        # 创建已发布对象
        obj1 = ObjectType(
            id=_uuid(),
            ontology_id=ontology.id,
            name="customer",
            display_name="客户",
            status=EntityStatus.PUBLISHED.value,
            segment_id=seg1_id,
        )
        # 创建草稿对象
        obj2 = ObjectType(
            id=_uuid(),
            ontology_id=ontology.id,
            name="order",
            display_name="订单",
            status=EntityStatus.SUGGESTED.value,
            segment_id=seg1_id,
        )
        db.add_all([obj1, obj2])

        seg1 = OntologySegment(
            id=seg1_id,
            ontology_id=ontology.id,
            name="customer_management",
            display_name="客户管理",
            member_count=2,
        )
        db.add(seg1)
        db.commit()

        service = OntologyQueryService()

        # published_only=True 应该只返回已发布对象
        result_published = service.get_ontology_grouped_graph(
            db, ontology.id, published_only=True
        )
        assert result_published.total_object_count == 1

        # published_only=False 应该返回所有对象
        result_all = service.get_ontology_grouped_graph(
            db, ontology.id, published_only=False
        )
        assert result_all.total_object_count == 2

    finally:
        db.rollback()
        db.close()


def _obj(ontology_id: str, name: str, display: str, *, segment_id: str | None = None,
         is_hub: bool = False) -> ObjectType:
    return ObjectType(
        id=_uuid(),
        ontology_id=ontology_id,
        name=name,
        display_name=display,
        status=EntityStatus.SUGGESTED.value,
        segment_id=segment_id,
        is_hub=is_hub,
    )


def _rel(ontology_id: str, src: ObjectType, dst: ObjectType, display: str) -> RelationType:
    return RelationType(
        id=_uuid(),
        ontology_id=ontology_id,
        source_object_type_id=src.id,
        target_object_type_id=dst.id,
        name=f"{src.name}_{dst.name}",
        display_name=display,
        status=EntityStatus.SUGGESTED.value,
    )


def test_segment_detail_always_returns_internal_edges():
    """板块内的边恒返回。

    早期只在成员 > 40 时才给边，结果最大的板块（32 成员 / 51 条内部关系）也拿不到，
    板块关系图从未渲染过一次——业务地图的主画面就是这张图，不能有门槛。
    """
    db = SessionLocal()
    try:
        ontology = _fresh_ontology(db)
        seg_id = _uuid()
        a = _obj(ontology.id, "invoice", "发票", segment_id=seg_id)
        b = _obj(ontology.id, "invoice_item", "发票明细", segment_id=seg_id)
        db.add_all([a, b])
        db.add(_rel(ontology.id, b, a, "属于"))
        db.add(
            OntologySegment(
                id=seg_id,
                ontology_id=ontology.id,
                name="billing",
                display_name="开票",
                member_count=2,
            )
        )
        db.commit()

        detail = OntologyQueryService().get_segment_detail(db, seg_id)
        assert detail is not None
        assert detail.internal_relation_count == 1
        assert [e.label for e in (detail.edges or [])] == ["属于"]
    finally:
        db.rollback()
        db.close()


def test_segment_detail_aggregates_cross_module_neighbors():
    """跨板块关系按外部对象聚合成邻居，而不是泼出一堆散点。

    同一个「公司」被引 N 次算一个邻居、N 条边；邻居按连接条数降序，
    截断发生在邻居这一层，边跟着邻居走。
    """
    db = SessionLocal()
    try:
        ontology = _fresh_ontology(db)
        seg_id, other_seg_id = _uuid(), _uuid()
        members = [
            _obj(ontology.id, f"doc{i}", f"单据{i}", segment_id=seg_id) for i in range(3)
        ]
        company = _obj(ontology.id, "company", "公司", is_hub=True)
        opportunity = _obj(ontology.id, "opportunity", "商机", segment_id=other_seg_id)
        db.add_all([*members, company, opportunity])
        # 三条指向同一个外部枢纽 + 一条指向另一板块的对象
        for m in members:
            db.add(_rel(ontology.id, m, company, "属于"))
        db.add(_rel(ontology.id, members[0], opportunity, "来自"))
        db.add_all(
            [
                OntologySegment(
                    id=seg_id,
                    ontology_id=ontology.id,
                    name="docs",
                    display_name="单据",
                    member_count=3,
                ),
                OntologySegment(
                    id=other_seg_id,
                    ontology_id=ontology.id,
                    name="crm",
                    display_name="营销与商机",
                    member_count=1,
                ),
            ]
        )
        db.commit()

        detail = OntologyQueryService().get_segment_detail(db, seg_id)
        assert detail is not None
        assert detail.internal_relation_count == 0
        assert detail.cross_relation_count == 4

        # 4 条跨板块关系聚成 2 个邻居，按连接条数降序
        assert [(n.display_name, n.link_count) for n in detail.neighbors] == [
            ("公司", 3),
            ("商机", 1),
        ]
        assert detail.neighbors[0].is_hub is True
        assert detail.neighbors[1].segment_name == "营销与商机"
        # 边跟着邻居走：每个邻居的每条关系都在
        assert len(detail.cross_edges) == 4
    finally:
        db.rollback()
        db.close()


def test_segment_detail_caps_neighbors_deterministically():
    """邻居数量封顶，且同一份数据每次返回同一批邻居（同分按 id 定序）。"""
    from app.services.ontology_query import _SEGMENT_NEIGHBOR_CAP

    db = SessionLocal()
    try:
        ontology = _fresh_ontology(db)
        seg_id = _uuid()
        member = _obj(ontology.id, "hub_doc", "主单据", segment_id=seg_id)
        outsiders = [
            _obj(ontology.id, f"ext{i}", f"外部对象{i}")
            for i in range(_SEGMENT_NEIGHBOR_CAP + 5)
        ]
        db.add_all([member, *outsiders])
        for o in outsiders:
            db.add(_rel(ontology.id, member, o, "引用"))
        db.add(
            OntologySegment(
                id=seg_id,
                ontology_id=ontology.id,
                name="docs",
                display_name="单据",
                member_count=1,
            )
        )
        db.commit()

        service = OntologyQueryService()
        first = service.get_segment_detail(db, seg_id)
        second = service.get_segment_detail(db, seg_id)
        assert first is not None and second is not None
        assert first.cross_relation_count == _SEGMENT_NEIGHBOR_CAP + 5
        assert len(first.neighbors) == _SEGMENT_NEIGHBOR_CAP
        assert len(first.cross_edges) == _SEGMENT_NEIGHBOR_CAP
        assert [n.id for n in first.neighbors] == [n.id for n in second.neighbors]
    finally:
        db.rollback()
        db.close()


def test_grouped_graph_reports_per_cluster_relation_counts():
    """每个板块自己的关系账：内部条数是板块目录的排序键。

    跨板块计数要覆盖「一端落在未分配对象上」的关系——那也是这块业务连出去的地方。
    """
    db = SessionLocal()
    try:
        ontology = _fresh_ontology(db)
        seg_id, other_seg_id = _uuid(), _uuid()
        a = _obj(ontology.id, "order", "订单", segment_id=seg_id)
        b = _obj(ontology.id, "order_item", "订单明细", segment_id=seg_id)
        c = _obj(ontology.id, "product", "产品", segment_id=other_seg_id)
        loose = _obj(ontology.id, "note", "备注")  # 未分配到任何板块
        db.add_all([a, b, c, loose])
        db.add(_rel(ontology.id, b, a, "属于"))  # 板块内
        db.add(_rel(ontology.id, b, c, "引用"))  # 跨板块
        db.add(_rel(ontology.id, a, loose, "附带"))  # 连向未分配对象，也算跨出去
        db.add_all(
            [
                OntologySegment(
                    id=seg_id,
                    ontology_id=ontology.id,
                    name="sales",
                    display_name="销售",
                    member_count=2,
                ),
                OntologySegment(
                    id=other_seg_id,
                    ontology_id=ontology.id,
                    name="catalog",
                    display_name="商品",
                    member_count=1,
                ),
            ]
        )
        db.commit()

        result = OntologyQueryService().get_ontology_grouped_graph(db, ontology.id)
        sales = next(c for c in result.clusters if c.name == "销售")
        catalog = next(c for c in result.clusters if c.name == "商品")
        assert sales.internal_relation_count == 1
        assert sales.cross_relation_count == 2
        assert catalog.internal_relation_count == 0
        assert catalog.cross_relation_count == 1
    finally:
        db.rollback()
        db.close()


def test_ontology_graph_carries_structure_type():
    """``GraphEdge.structure_type`` 必须原样带出来，两个构造分支都要。

    它曾经在两处都漏填，于是这个字段恒为 None：数据加工血缘（derivation）和业务关系
    （外键/引用）在图上长得一模一样，血缘视角因此读不出「数据从哪来」，只能把外键
    当成来源。字段本来就在 schema 上——漏填不报错，只是静默地把区分能力抹掉。
    """
    db = SessionLocal()
    try:
        ontology = _fresh_ontology(db)
        ods = ObjectType(
            id=_uuid(), ontology_id=ontology.id, name="ods_order",
            display_name="ODS订单", status=EntityStatus.SUGGESTED.value,
        )
        dwd = ObjectType(
            id=_uuid(), ontology_id=ontology.id, name="dwd_order",
            display_name="DWD订单", status=EntityStatus.SUGGESTED.value,
        )
        customer = ObjectType(
            id=_uuid(), ontology_id=ontology.id, name="customer",
            display_name="客户", status=EntityStatus.SUGGESTED.value,
        )
        # 只为把 ods 的度数抬到 2：截断分支按度数排名保留邻居，度数打平会让结果随集合
        # 迭代顺序摆动，测试就成了偶尔失败的那种。
        raw = ObjectType(
            id=_uuid(), ontology_id=ontology.id, name="raw_order",
            display_name="源库订单", status=EntityStatus.SUGGESTED.value,
        )
        db.add_all([ods, dwd, customer, raw])
        db.flush()
        db.add_all([
            RelationType(
                id=_uuid(), ontology_id=ontology.id, name="ods_to_dwd",
                display_name="加工为", source_object_type_id=ods.id,
                target_object_type_id=dwd.id, structure_type="derivation",
                status=EntityStatus.SUGGESTED.value,
            ),
            RelationType(
                id=_uuid(), ontology_id=ontology.id, name="dwd_of_customer",
                display_name="归属客户", source_object_type_id=dwd.id,
                target_object_type_id=customer.id, structure_type="foreign_key",
                status=EntityStatus.SUGGESTED.value,
            ),
            RelationType(
                id=_uuid(), ontology_id=ontology.id, name="raw_to_ods",
                display_name="装载为", source_object_type_id=raw.id,
                target_object_type_id=ods.id, structure_type="derivation",
                status=EntityStatus.SUGGESTED.value,
            ),
        ])
        db.commit()

        service = OntologyQueryService()
        # 全量分支（对象数未超上限）
        full = service.get_ontology_graph(db, ontology.id)
        by_label = {edge.label: edge.structure_type for edge in full.edges}
        assert by_label == {
            "加工为": "derivation",
            "归属客户": "foreign_key",
            "装载为": "derivation",
        }

        # BFS 截断分支：压低上限强制走 center + 邻域那条路径。ods 度数更高，
        # 截断后必然留它，加工血缘那条边因此可预期地出现在结果里。
        neighborhood = service.get_ontology_graph(
            db, ontology.id, center_id=dwd.id, depth=1, max_nodes=2
        )
        assert neighborhood.truncated is True
        assert [e.structure_type for e in neighborhood.edges] == ["derivation"]
    finally:
        db.close()
