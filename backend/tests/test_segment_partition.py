"""板块划分是全覆盖分区：每个对象恰好属于一个板块，没有「未接入」这一桶。

实测起点（erpnext 本体 1035 对象）：890 个未接入，占 86%。那不是一个桶，
是四种处置方式完全不同的情况被混在了一起——见 services/segment_kinds。
"""

import uuid

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import (
    DomainContext,
    EntityStatus,
    ObjectType,
    Ontology,
    OntologySegment,
    OntologyStatus,
    RelationType,
)
from app.schemas import DraftObjectType, DraftSegment
from app.services.ontology_merge import MergeReport, OntologyMergeService
from app.services.segment_generator import build_fallback_segments
from app.services.segment_kinds import (
    SEGMENT_KIND_BUSINESS,
    SEGMENT_KIND_PENDING,
    SEGMENT_KIND_SHARED,
    SEGMENT_KIND_SYSTEM,
    SEGMENT_KIND_TECHNICAL,
    is_system_table,
    schema_of_source_ref,
)


def _uuid() -> str:
    return str(uuid.uuid4())


def _urn(schema: str, table: str) -> str:
    return f"urn:li:dataset:(urn:li:dataPlatform:mysql,{schema}.{table},PROD)"


def _draft_obj(name: str, role: str, *, schema: str = "erp_db") -> DraftObjectType:
    return DraftObjectType(
        name=name,
        display_name=name,
        table_role=role,
        source_ref=_urn(schema, name),
    )


# ---------------------------------------------------------------- URN 解析


def test_schema_parsing_recognises_system_schemas():
    assert schema_of_source_ref(_urn("mysql", "column_stats")) == "mysql"
    assert is_system_table(_urn("performance_schema", "events_waits_current"))
    assert is_system_table(_urn("information_schema", "tables"))
    assert is_system_table(
        "urn:li:dataset:(urn:li:dataPlatform:postgres,mydb.pg_catalog.pg_class,PROD)"
    )
    # 业务库不能被误杀——宁可漏判也不能把业务对象扫进系统表
    assert not is_system_table(_urn("_d71df877e93eac81", "tabItem"))
    assert not is_system_table(None)
    assert not is_system_table("garbage")


# ---------------------------------------------------------------- 兜底分派


def test_fallback_segments_cover_every_unclaimed_object():
    """没进业务模块的对象一个不落，且按原因分开。"""
    objects = [
        _draft_obj("customer", "business_object"),
        _draft_obj("order", "business_object"),
        _draft_obj("company", "business_object"),  # 枢纽
        _draft_obj("lonely_table", "business_object"),  # 有资格但连不成簇
        _draft_obj("child_item", "bridge"),  # 同上
        _draft_obj("web_form", "technical"),  # 框架管道
        _draft_obj("column_stats", "technical", schema="mysql"),  # 系统表
        _draft_obj("events_waits", "business_object", schema="performance_schema"),
    ]
    business = [
        DraftSegment(
            name="sales",
            display_name="销售",
            kind=SEGMENT_KIND_BUSINESS,
            member_count=2,
            members=["customer", "order"],
        )
    ]

    fallbacks = build_fallback_segments(objects, business, {"company"})
    by_kind = {seg.kind: seg for seg in fallbacks}

    assert set(by_kind) == {
        SEGMENT_KIND_SHARED,
        SEGMENT_KIND_PENDING,
        SEGMENT_KIND_TECHNICAL,
        SEGMENT_KIND_SYSTEM,
    }
    assert by_kind[SEGMENT_KIND_SHARED].members == ["company"]
    assert sorted(by_kind[SEGMENT_KIND_PENDING].members) == ["child_item", "lonely_table"]
    assert by_kind[SEGMENT_KIND_TECHNICAL].members == ["web_form"]
    # 系统表优先于角色判定：`events_waits` 被判成 business_object 也照样归系统表，
    # 因为「不该进本体」比「是什么角色」更能说明该怎么处置它。
    assert sorted(by_kind[SEGMENT_KIND_SYSTEM].members) == ["column_stats", "events_waits"]

    # 全覆盖：业务模块 + 兜底 = 全部对象，且互不重叠
    placed = [n for seg in business + fallbacks for n in seg.members]
    assert sorted(placed) == sorted(o.name for o in objects)
    assert len(placed) == len(set(placed))


def test_fallback_segments_skip_empty_buckets():
    """没有系统表的本体不该凭空多出一个「系统表」板块。"""
    objects = [_draft_obj("customer", "business_object"), _draft_obj("order", "business_object")]
    business = [
        DraftSegment(
            name="sales",
            display_name="销售",
            kind=SEGMENT_KIND_BUSINESS,
            member_count=2,
            members=["customer", "order"],
        )
    ]
    assert build_fallback_segments(objects, business, set()) == []


def test_fallback_segments_are_not_business_kind():
    """兜底板块必须带非 business 的 kind：LLM 命名与去重都靠它跳过。"""
    objects = [_draft_obj("web_form", "technical")]
    fallbacks = build_fallback_segments(objects, [], set())
    assert fallbacks and all(seg.kind != SEGMENT_KIND_BUSINESS for seg in fallbacks)
    # 固定标识名是下一轮合并的对齐键，不能为空
    assert all(seg.name and seg.display_name for seg in fallbacks)


# ---------------------------------------------------------------- 落库


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


def _seed(db: Session, ontology_id: str, names: dict[str, str]) -> None:
    for name, role in names.items():
        db.add(
            ObjectType(
                id=_uuid(),
                ontology_id=ontology_id,
                name=name,
                display_name=name,
                table_role=role,
                status=EntityStatus.SUGGESTED.value,
            )
        )
    db.flush()


def test_merge_leaves_no_object_without_a_segment():
    """合并之后不该有任何对象 segment_id 为空——包括枢纽。"""
    db = SessionLocal()
    try:
        ontology = _fresh_ontology(db)
        _seed(
            db,
            ontology.id,
            {"customer": "business_object", "order": "business_object", "company": "business_object",
             "web_form": "technical"},
        )
        db.commit()

        segments = [
            DraftSegment(
                name="sales",
                display_name="销售",
                kind=SEGMENT_KIND_BUSINESS,
                member_count=2,
                members=["customer", "order"],
            ),
            DraftSegment(
                name="__shared_master_data__",
                display_name="公共主数据",
                kind=SEGMENT_KIND_SHARED,
                member_count=1,
                members=["company"],
            ),
            DraftSegment(
                name="__technical_tables__",
                display_name="技术表",
                kind=SEGMENT_KIND_TECHNICAL,
                member_count=1,
                members=["web_form"],
            ),
        ]
        objects = [
            _draft_obj("customer", "business_object"),
            _draft_obj("order", "business_object"),
            _draft_obj("company", "business_object"),
            _draft_obj("web_form", "technical"),
        ]

        service = OntologyMergeService()
        service.merge_segments(db, ontology.id, segments, None, MergeReport())
        db.flush()
        service._assign_segment_members(db, ontology.id, segments, ["company"], objects)
        db.flush()

        rows = db.query(ObjectType).filter(ObjectType.ontology_id == ontology.id).all()
        assert [r.name for r in rows if r.segment_id is None] == []

        # 枢纽既是事实判定，也有归属——两者正交，不该互相覆盖
        company = next(r for r in rows if r.name == "company")
        assert company.is_hub is True
        shared = db.query(OntologySegment).filter(
            OntologySegment.ontology_id == ontology.id,
            OntologySegment.kind == SEGMENT_KIND_SHARED,
        ).one()
        assert company.segment_id == shared.id
    finally:
        db.rollback()
        db.close()


def test_fallback_segment_name_survives_regeneration():
    """重跑不该每次多出一个空的兜底板块：固定标识名是对齐键，不能被加后缀。"""
    db = SessionLocal()
    try:
        ontology = _fresh_ontology(db)
        _seed(db, ontology.id, {"web_form": "technical"})
        db.commit()

        draft = [
            DraftSegment(
                name="__technical_tables__",
                display_name="技术表",
                kind=SEGMENT_KIND_TECHNICAL,
                member_count=1,
                members=["web_form"],
            )
        ]
        service = OntologyMergeService()
        for _ in range(3):
            service.merge_segments(db, ontology.id, draft, None, MergeReport())
            db.flush()

        rows = db.query(OntologySegment).filter(
            OntologySegment.ontology_id == ontology.id,
            OntologySegment.kind == SEGMENT_KIND_TECHNICAL,
        ).all()
        assert [r.name for r in rows] == ["__technical_tables__"]
    finally:
        db.rollback()
        db.close()


def test_grouped_graph_counts_hub_relations_against_shared_segment():
    """枢纽的关系要记到「公共主数据」头上。

    宏观图里枢纽是独立节点（不并进板块），关系账却必须按板块归属算——
    两张映射混用会让公共主数据显示成 0 关系，而它恰恰是全图连接最密的一块。
    """
    from app.services.ontology_query import OntologyQueryService

    db = SessionLocal()
    try:
        ontology = _fresh_ontology(db)
        seg_id, shared_id = _uuid(), _uuid()
        company = ObjectType(
            id=_uuid(), ontology_id=ontology.id, name="company", display_name="公司",
            status=EntityStatus.SUGGESTED.value, segment_id=shared_id, is_hub=True,
        )
        currency = ObjectType(
            id=_uuid(), ontology_id=ontology.id, name="currency", display_name="币种",
            status=EntityStatus.SUGGESTED.value, segment_id=shared_id, is_hub=True,
        )
        order = ObjectType(
            id=_uuid(), ontology_id=ontology.id, name="order", display_name="订单",
            status=EntityStatus.SUGGESTED.value, segment_id=seg_id,
        )
        db.add_all([company, currency, order])
        db.add(RelationType(
            id=_uuid(), ontology_id=ontology.id, source_object_type_id=company.id,
            target_object_type_id=currency.id, name="uses", display_name="使用",
            status=EntityStatus.SUGGESTED.value,
        ))
        db.add(RelationType(
            id=_uuid(), ontology_id=ontology.id, source_object_type_id=order.id,
            target_object_type_id=company.id, name="belongs", display_name="属于",
            status=EntityStatus.SUGGESTED.value,
        ))
        db.add_all([
            OntologySegment(id=seg_id, ontology_id=ontology.id, name="sales",
                            display_name="销售", kind=SEGMENT_KIND_BUSINESS, member_count=1),
            OntologySegment(id=shared_id, ontology_id=ontology.id, name="__shared_master_data__",
                            display_name="公共主数据", kind=SEGMENT_KIND_SHARED, member_count=2),
        ])
        db.commit()

        result = OntologyQueryService().get_ontology_grouped_graph(db, ontology.id)
        shared = next(c for c in result.clusters if c.kind == SEGMENT_KIND_SHARED)
        assert shared.internal_relation_count == 1  # company — currency
        assert shared.cross_relation_count == 1  # order — company
        assert result.isolated_nodes == []
    finally:
        db.rollback()
        db.close()


def test_place_unsegmented_is_idempotent_and_covers_new_objects():
    """对象不只从生成流水线进来：人工建模/派生/编辑都会直接 new 一个 ObjectType。

    只在生成时保证不变量，界面上就会慢慢又冒出「未接入板块 N」——实测跑了一轮后台
    重生成加两个派生对象就漏了 10 个。所以落库入口只有 place_unsegmented 一个，
    且必须幂等：重复调用不产生第二个「系统表」板块，也不动已归属的对象。
    """
    from app.services.segment_placement import place_unsegmented

    db = SessionLocal()
    try:
        ontology = _fresh_ontology(db)
        seg_id = _uuid()
        db.add(
            OntologySegment(
                id=seg_id, ontology_id=ontology.id, name="sales", display_name="销售",
                kind=SEGMENT_KIND_BUSINESS, member_count=1,
            )
        )
        placed_obj = ObjectType(
            id=_uuid(), ontology_id=ontology.id, name="order", display_name="订单",
            table_role="business_object", status=EntityStatus.SUGGESTED.value, segment_id=seg_id,
        )
        # 之后才被创建的三个对象：各走一条兜底路径
        newcomers = [
            ObjectType(
                id=_uuid(), ontology_id=ontology.id, name="processlist", display_name="进程列表",
                table_role="technical", status=EntityStatus.SUGGESTED.value,
                source_ref=_urn("sys", "processlist"),
            ),
            ObjectType(
                id=_uuid(), ontology_id=ontology.id, name="web_form", display_name="网页表单",
                table_role="technical", status=EntityStatus.SUGGESTED.value,
                source_ref=_urn("erp_db", "web_form"),
            ),
            ObjectType(
                id=_uuid(), ontology_id=ontology.id, name="wide_order", display_name="订单宽表",
                table_role="business_object", status=EntityStatus.EDITED.value,
                source_ref=f"derived:{ontology.id}:wide_order",
            ),
        ]
        db.add_all([placed_obj, *newcomers])
        db.commit()

        first = place_unsegmented(db, ontology.id)
        assert first == {
            SEGMENT_KIND_PENDING: 1,
            SEGMENT_KIND_TECHNICAL: 1,
            SEGMENT_KIND_SYSTEM: 1,
        }
        db.flush()
        assert (
            db.query(ObjectType)
            .filter(ObjectType.ontology_id == ontology.id, ObjectType.segment_id.is_(None))
            .count()
            == 0
        )
        # 已归属的对象不被挪动
        assert placed_obj.segment_id == seg_id

        # 幂等：再跑一次什么都不做，也不多建板块
        assert place_unsegmented(db, ontology.id) == {}
        assert (
            db.query(OntologySegment)
            .filter(
                OntologySegment.ontology_id == ontology.id,
                OntologySegment.kind == SEGMENT_KIND_SYSTEM,
            )
            .count()
            == 1
        )
    finally:
        db.rollback()
        db.close()


def test_review_stats_reports_relation_and_role_scoped_counts():
    """审核侧栏的三种口径各算各的，不互相顶替。

    关系页此前只显示板块名不给数字，因为拿对象进度当关系进度会读成假数字；
    关系表（bridge）挪到关系页单独审后，侧栏要的又是「这个板块还剩几张关系表」。
    三个口径缺一个，那一页的数字就只能空着。
    """
    from app.services.ontology_query import OntologyQueryService

    db = SessionLocal()
    try:
        ontology = _fresh_ontology(db)
        seg_id = _uuid()
        order = ObjectType(
            id=_uuid(), ontology_id=ontology.id, name="order", display_name="订单",
            table_role="business_object", status=EntityStatus.SUGGESTED.value,
            segment_id=seg_id, needs_review=True,
        )
        link = ObjectType(
            id=_uuid(), ontology_id=ontology.id, name="order_item", display_name="订单明细",
            table_role="bridge", status=EntityStatus.SUGGESTED.value,
            segment_id=seg_id, needs_review=True,
        )
        done = ObjectType(
            id=_uuid(), ontology_id=ontology.id, name="customer", display_name="客户",
            table_role="business_object", status=EntityStatus.SUGGESTED.value,
            segment_id=seg_id, needs_review=False,
        )
        db.add_all([order, link, done])
        db.add(
            OntologySegment(
                id=seg_id, ontology_id=ontology.id, name="sales", display_name="销售",
                kind=SEGMENT_KIND_BUSINESS, member_count=3,
            )
        )
        # 关系归到**源端对象**的板块
        db.add(RelationType(
            id=_uuid(), ontology_id=ontology.id, source_object_type_id=order.id,
            target_object_type_id=done.id, name="belongs", display_name="属于",
            status=EntityStatus.SUGGESTED.value, needs_review=True,
        ))
        db.commit()

        stats = OntologyQueryService().get_review_mode_stats(db, ontology.id)
        assert stats.pending_by_role == {"business_object": 1, "bridge": 1}
        assert stats.total_by_role == {"business_object": 2, "bridge": 1}

        seg = next(s for s in stats.segment_progress if s.segment_id == seg_id)
        # 对象口径（含全部角色）
        assert (seg.total_count, seg.needs_review_count) == (3, 2)
        # 角色拆分：对象页只数非 bridge，关系表页只数 bridge
        assert seg.role_total == {"business_object": 2, "bridge": 1}
        assert seg.role_pending == {"business_object": 1, "bridge": 1}
        # 关系口径
        assert (seg.relation_total, seg.relation_needs_review) == (1, 1)
    finally:
        db.rollback()
        db.close()


def test_review_queue_role_filter_splits_bridge_out_of_the_object_queue():
    """关系表不该出现在对象队列里：两种判断标准挤同一屏会互相打架。"""
    from app.services.ontology_query import OntologyQueryService

    db = SessionLocal()
    try:
        ontology = _fresh_ontology(db)
        db.add_all([
            ObjectType(
                id=_uuid(), ontology_id=ontology.id, name=f"obj{i}", display_name=f"对象{i}",
                table_role=role, status=EntityStatus.SUGGESTED.value, needs_review=True,
            )
            for i, role in enumerate(["business_object", "bridge", "technical", "bridge"])
        ])
        db.commit()

        service = OntologyQueryService()
        objects = service.get_review_queue(
            db, ontology.id, role_in=["business_object", "data_table", "technical"]
        )
        bridges = service.get_review_queue(db, ontology.id, role_in=["bridge"])
        assert objects.pending_total == 2
        assert bridges.pending_total == 2
        assert set(objects.pending_by_role) == {"business_object", "technical"}
        assert set(bridges.pending_by_role) == {"bridge"}
    finally:
        db.rollback()
        db.close()
