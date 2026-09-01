"""板块生成与合并：第二遍聚类、锚点匹配、三方合并验证。"""

import json

import pytest

from app.database import Base, SessionLocal, engine
from app.models import DomainContext, ObjectType, Ontology, OntologySegment, OntologyStatus
from app.schemas import DraftObjectType, DraftRelationType, DraftSegment, OntologyDraftOutput
from app.services.ontology_merge import MergeReport, OntologyMergeService
from app.services.segment_generator import generate_segments


@pytest.fixture(scope="module", autouse=True)
def _tables():
    Base.metadata.create_all(bind=engine)
    yield


def _fresh_ontology(db) -> Ontology:
    import uuid

    domain = DomainContext(datahub_domain_id=f"urn:test:{uuid.uuid4()}", name="测试域")
    db.add(domain)
    db.flush()
    ontology = Ontology(
        domain_context_id=domain.id, status=OntologyStatus.DRAFT.value, generated_by="test"
    )
    db.add(ontology)
    db.flush()
    return ontology


def test_segment_generation_basic():
    """测试基础的板块生成：聚类、枢纽识别、成员分配。"""
    # 构造一个更大的本体：10+ 个业务对象，1 个高度数的枢纽对象
    # 枢纽识别的阈值是 max(15, mean_degree * 3)，所以需要足够多的节点
    objects = []
    for i in range(20):
        objects.append(
            DraftObjectType(
                name=f"table_{i}",
                display_name=f"表{i}",
                source_ref=f"urn:li:dataset:table_{i}",
                table_role="business_object",
            )
        )
    # 添加一个枢纽对象
    objects.append(
        DraftObjectType(
            name="company",
            display_name="公司",
            source_ref="urn:li:dataset:company",
            table_role="business_object",
        )
    )

    relations = []
    # company 连接到所有其他对象（度数 = 20）
    for i in range(20):
        relations.append(
            DraftRelationType(
                name=f"table_{i}_to_company",
                display_name="隶属于",
                source_object_type_name=f"table_{i}",
                target_object_type_name="company",
                structure_type="many_to_one",
            )
        )
    # 其他对象之间形成几个小簇
    for i in range(0, 18, 3):
        relations.append(
            DraftRelationType(
                name=f"rel_{i}_{i+1}",
                display_name="关联",
                source_object_type_name=f"table_{i}",
                target_object_type_name=f"table_{i+1}",
                structure_type="one_to_many",
            )
        )
        relations.append(
            DraftRelationType(
                name=f"rel_{i+1}_{i+2}",
                display_name="包含",
                source_object_type_name=f"table_{i+1}",
                target_object_type_name=f"table_{i+2}",
                structure_type="one_to_many",
            )
        )

    segments, hub_nodes = generate_segments(objects, relations, llm_client=None)

    # 验证：company 应该被识别为枢纽（度数远高于平均）
    assert "company" in hub_nodes

    # 验证：剩余对象应该形成若干板块
    assert len(segments) > 0


def test_segment_merge_with_anchor_matching():
    """测试板块合并：锚点匹配、三方合并、人工修正保护。"""
    merge = OntologyMergeService()
    db = SessionLocal()
    try:
        ontology = _fresh_ontology(db)

        # 第一次生成：创建一个板块
        seg1 = DraftSegment(
            name="order_management",
            display_name="订单管理",
            description="订单相关业务",
            anchor_refs=["urn:li:dataset:order", "urn:li:dataset:order_item"],
            member_count=3,
            machine_baseline="订单",
            members=["order", "order_item", "payment"],
        )

        report = MergeReport()
        merge.merge_segments(db, ontology.id, [seg1], "gen1", report)
        db.commit()

        # 验证板块创建成功
        segments = db.query(OntologySegment).filter(
            OntologySegment.ontology_id == ontology.id
        ).all()
        assert len(segments) == 1
        assert segments[0].name == "order_management"
        assert segments[0].display_name == "订单管理"
        assert json.loads(segments[0].anchor_refs or "[]") == [
            "urn:li:dataset:order",
            "urn:li:dataset:order_item",
        ]
        assert report.to_dict()["summary"]["added"] == 1

        # 人工修改 display_name
        seg_id = segments[0].id
        segments[0].display_name = "订单域"
        segments[0].overridden_fields = json.dumps(["display_name"])
        db.commit()

        # 第二次生成：锚点完全相同，应该匹配到同一板块
        seg2 = DraftSegment(
            name="order_management",
            display_name="订单管理板块",  # 机器给了新名字
            description="订单相关业务（更新）",
            anchor_refs=["urn:li:dataset:order", "urn:li:dataset:order_item"],
            member_count=3,
            machine_baseline="订单",
            members=["order", "order_item", "payment"],
        )

        report2 = MergeReport()
        merge.merge_segments(db, ontology.id, [seg2], "gen2", report2)
        db.commit()

        # 验证：人工修改的 display_name 应该被保留
        segments = db.query(OntologySegment).filter(
            OntologySegment.ontology_id == ontology.id
        ).all()
        assert len(segments) == 1
        assert segments[0].id == seg_id  # 同一个板块
        assert segments[0].display_name == "订单域"  # 人工值保留
        assert segments[0].description == "订单相关业务（更新）"  # 机器值更新
        # 因为 display_name 产生了冲突（人工改过且机器也改了），outcome 是 conflict
        assert report2.to_dict()["summary"]["conflict"] == 1

    finally:
        db.rollback()
        db.close()


def test_segment_anchor_jaccard_matching():
    """测试锚点 Jaccard 匹配：重叠度 >= 0.5 判为同一板块。"""
    merge = OntologyMergeService()
    db = SessionLocal()
    try:
        ontology = _fresh_ontology(db)

        # 第一次生成：3 个锚点
        seg1 = DraftSegment(
            name="sales",
            display_name="销售",
            anchor_refs=["urn:a", "urn:b", "urn:c"],
            member_count=5,
            members=["a", "b", "c", "d", "e"],
        )

        report = MergeReport()
        merge.merge_segments(db, ontology.id, [seg1], "gen1", report)
        db.commit()

        seg_id = db.query(OntologySegment).filter(
            OntologySegment.ontology_id == ontology.id
        ).one().id

        # 第二次生成：2 个锚点重叠（Jaccard = 2/4 = 0.5，恰好达到阈值）
        seg2 = DraftSegment(
            name="sales_updated",
            display_name="销售板块",
            anchor_refs=["urn:a", "urn:b", "urn:x"],  # 2 个相同，1 个新增
            member_count=6,
            members=["a", "b", "c", "d", "e", "x"],
        )

        report2 = MergeReport()
        merge.merge_segments(db, ontology.id, [seg2], "gen2", report2)
        db.commit()

        # 验证：应该匹配到同一板块
        segments = db.query(OntologySegment).filter(
            OntologySegment.ontology_id == ontology.id
        ).all()
        assert len(segments) == 1
        assert segments[0].id == seg_id
        assert report2.to_dict()["summary"]["updated"] == 1

        # 第三次生成：只有 1 个锚点重叠（Jaccard = 1/4 = 0.25，低于阈值）
        # 这次传入单独的 seg3，旧板块 sales_updated 因为未匹配会被移除
        seg3 = DraftSegment(
            name="marketing",
            display_name="营销",
            anchor_refs=["urn:a", "urn:y", "urn:z"],  # 只 1 个相同
            member_count=4,
            members=["a", "y", "z", "w"],
        )

        report3 = MergeReport()
        merge.merge_segments(db, ontology.id, [seg3], "gen3", report3)
        db.commit()

        # 验证：sales_updated 被移除（纯机器板块），marketing 被创建
        segments = db.query(OntologySegment).filter(
            OntologySegment.ontology_id == ontology.id
        ).all()
        assert len(segments) == 1
        assert segments[0].name == "marketing"
        assert report3.to_dict()["summary"]["added"] == 1
        assert report3.to_dict()["summary"]["removed"] == 1

    finally:
        db.rollback()
        db.close()


def test_segment_removal_handling():
    """测试板块移除：纯机器板块删除，含人工修正的标记 upstream_removed。"""
    merge = OntologyMergeService()
    db = SessionLocal()
    try:
        ontology = _fresh_ontology(db)

        # 创建两个板块：一个纯机器，一个人工修改过
        seg1 = DraftSegment(
            name="seg1",
            display_name="板块1",
            anchor_refs=["urn:a"],
            member_count=1,
            members=["a"],
        )
        seg2 = DraftSegment(
            name="seg2",
            display_name="板块2",
            anchor_refs=["urn:b"],
            member_count=1,
            members=["b"],
        )

        report = MergeReport()
        merge.merge_segments(db, ontology.id, [seg1, seg2], "gen1", report)
        db.commit()

        segments = db.query(OntologySegment).filter(
            OntologySegment.ontology_id == ontology.id
        ).all()
        assert len(segments) == 2

        # 人工修改第二个板块
        segments[1].display_name = "板块2（改）"
        segments[1].overridden_fields = json.dumps(["display_name"])
        db.commit()

        # 第二次生成：只有 seg1，seg2 消失了
        seg1_updated = DraftSegment(
            name="seg1",
            display_name="板块1",
            anchor_refs=["urn:a"],
            member_count=1,
            members=["a"],
        )

        report2 = MergeReport()
        merge.merge_segments(db, ontology.id, [seg1_updated], "gen2", report2)
        db.commit()

        # 验证：seg1 保留，seg2 应该被标记 upstream_removed 而非删除
        segments = db.query(OntologySegment).filter(
            OntologySegment.ontology_id == ontology.id
        ).all()
        assert len(segments) == 2  # 都还在

        seg2_after = next(s for s in segments if s.name == "seg2")
        assert seg2_after.upstream_removed is True  # 标记了
        assert seg2_after.display_name == "板块2（改）"  # 人工值保留

    finally:
        db.rollback()
        db.close()
