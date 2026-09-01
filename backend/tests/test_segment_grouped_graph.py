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
