"""测试对象角色提升时的自动板块分配（邻居投票）"""

import pytest
from sqlalchemy.orm import Session

from app.database import Base, SessionLocal, engine
from app.models import DomainContext, ObjectType, Ontology, OntologySegment, RelationType
from app.services.edit import EditService
from app.services.segment_kinds import SEGMENT_KIND_SYSTEM


@pytest.fixture(scope="module", autouse=True)
def _tables():
    Base.metadata.create_all(bind=engine)
    yield


def _setup_ontology_with_segments(db: Session):
    """创建一个带板块的测试本体"""
    import uuid

    domain = DomainContext(datahub_domain_id=f"urn:test:{uuid.uuid4()}", name="测试域")
    db.add(domain)
    db.flush()

    ontology = Ontology(
        domain_context_id=domain.id, status="draft", generated_by="test"
    )
    db.add(ontology)
    db.flush()

    # 创建两个板块
    seg1 = OntologySegment(
        ontology_id=ontology.id,
        name="sales",
        display_name="销售板块",
        member_count=2,
    )
    seg2 = OntologySegment(
        ontology_id=ontology.id,
        name="inventory",
        display_name="库存板块",
        member_count=2,
    )
    db.add(seg1)
    db.add(seg2)
    db.flush()

    # 创建四个业务对象，分别归属不同板块
    order = ObjectType(
        ontology_id=ontology.id,
        name="order",
        display_name="订单",
        table_role="business_object",
        segment_id=seg1.id,
    )
    customer = ObjectType(
        ontology_id=ontology.id,
        name="customer",
        display_name="客户",
        table_role="business_object",
        segment_id=seg1.id,
    )
    product = ObjectType(
        ontology_id=ontology.id,
        name="product",
        display_name="产品",
        table_role="business_object",
        segment_id=seg2.id,
    )
    warehouse = ObjectType(
        ontology_id=ontology.id,
        name="warehouse",
        display_name="仓库",
        table_role="business_object",
        segment_id=seg2.id,
    )

    # 创建一个待提升的数据表对象
    order_item = ObjectType(
        ontology_id=ontology.id,
        name="order_item",
        display_name="订单明细",
        table_role="data_table",  # 初始为 data_table
        segment_id=None,
    )

    db.add_all([order, customer, product, warehouse, order_item])
    db.flush()

    # 建立关系：order_item 与 order 和 product 相连
    rel1 = RelationType(
        ontology_id=ontology.id,
        name="order_to_item",
        display_name="包含",
        source_object_type_id=order.id,
        target_object_type_id=order_item.id,
        structure_type="one_to_many",
    )
    rel2 = RelationType(
        ontology_id=ontology.id,
        name="item_to_product",
        display_name="引用",
        source_object_type_id=order_item.id,
        target_object_type_id=product.id,
        structure_type="many_to_one",
    )
    db.add_all([rel1, rel2])
    db.flush()

    return ontology, order_item, seg1, seg2


def test_auto_assign_segment_on_role_promotion():
    """测试：将对象提升为 business_object 时自动分配板块"""
    db = SessionLocal()
    try:
        ontology, order_item, seg1, seg2 = _setup_ontology_with_segments(db)
        db.commit()

        # 初始状态：order_item 是 data_table，无板块
        assert order_item.table_role == "data_table"
        assert order_item.segment_id is None

        # 提升为 business_object
        edit_service = EditService()
        result = edit_service.update_object_type(
            db, order_item.id, table_role="business_object", operator="test"
        )

        # 验证：应该自动分配到板块
        # order_item 连接到 order (seg1) 和 product (seg2)，票数相同
        # 按稳定 hash 排序应该选择其中一个
        db.refresh(order_item)
        assert order_item.table_role == "business_object"
        assert order_item.segment_id is not None
        assert order_item.segment_id in (seg1.id, seg2.id)

    finally:
        db.rollback()
        db.close()


def test_auto_assign_prefers_majority_segment():
    """测试：邻居投票选择票数最多的板块"""
    db = SessionLocal()
    try:
        ontology, _, seg1, seg2 = _setup_ontology_with_segments(db)

        # 创建一个新的待提升对象
        payment = ObjectType(
            ontology_id=ontology.id,
            name="payment",
            display_name="支付",
            table_role="data_table",
            segment_id=None,
        )
        db.add(payment)
        db.flush()

        # 连接到销售板块的两个对象
        order = (
            db.query(ObjectType)
            .filter(ObjectType.ontology_id == ontology.id, ObjectType.name == "order")
            .one()
        )
        customer = (
            db.query(ObjectType)
            .filter(ObjectType.ontology_id == ontology.id, ObjectType.name == "customer")
            .one()
        )

        rel1 = RelationType(
            ontology_id=ontology.id,
            name="order_to_payment",
            display_name="关联",
            source_object_type_id=order.id,
            target_object_type_id=payment.id,
            structure_type="one_to_one",
        )
        rel2 = RelationType(
            ontology_id=ontology.id,
            name="customer_to_payment",
            display_name="发起",
            source_object_type_id=customer.id,
            target_object_type_id=payment.id,
            structure_type="one_to_many",
        )
        db.add_all([rel1, rel2])
        db.commit()

        # 提升为 business_object
        edit_service = EditService()
        edit_service.update_object_type(
            db, payment.id, table_role="business_object", operator="test"
        )

        # 验证：应该分配到 seg1（销售板块），因为有 2 票 vs 0 票
        db.refresh(payment)
        assert payment.segment_id == seg1.id

    finally:
        db.rollback()
        db.close()


def test_explicit_segment_overrides_auto_assign():
    """测试：显式设置板块时不触发自动分配"""
    db = SessionLocal()
    try:
        ontology, _, seg1, seg2 = _setup_ontology_with_segments(db)

        # 创建待提升对象
        shipment = ObjectType(
            ontology_id=ontology.id,
            name="shipment",
            display_name="发货",
            table_role="data_table",
            segment_id=None,
        )
        db.add(shipment)
        db.commit()

        # 同时提升角色并显式指定板块
        edit_service = EditService()
        edit_service.update_object_type(
            db,
            shipment.id,
            table_role="business_object",
            segment_id=seg2.id,
            operator="test",
        )

        # 验证：应该使用显式指定的板块，而不是自动分配
        db.refresh(shipment)
        assert shipment.segment_id == seg2.id

    finally:
        db.rollback()
        db.close()


def test_falls_back_to_the_system_board_when_no_neighbors():
    """测试：既无邻居也无同族时落系统表，而不是留成「没有板块」。

    每个对象恰好属于一个板块是硬不变量——留成 ``segment_id=None`` 等于让它从所有
    板块视图里消失。归不进业务模块的业务对象落系统表，由人在审核台上移出来。
    """
    db = SessionLocal()
    try:
        ontology, _, seg1, seg2 = _setup_ontology_with_segments(db)

        # 创建一个孤立对象
        isolated = ObjectType(
            ontology_id=ontology.id,
            name="isolated",
            display_name="孤立对象",
            table_role="data_table",
            segment_id=None,
        )
        db.add(isolated)
        db.commit()

        # 提升为 business_object
        edit_service = EditService()
        edit_service.update_object_type(
            db, isolated.id, table_role="business_object", operator="test"
        )

        # 验证：落在系统表板块里，不是无板块
        db.refresh(isolated)
        assert isolated.segment_id is not None
        assert db.get(OntologySegment, isolated.segment_id).kind == SEGMENT_KIND_SYSTEM

    finally:
        db.rollback()
        db.close()
