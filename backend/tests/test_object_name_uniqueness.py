"""对象标识名在本体内唯一：库层约束 + 各写入路径的前置守卫。

``name`` 是 Agent 写 SQL 用的标识符，也是 ``ontology_projection`` 建索引的键——重名会
让解析静默只命中其中一个。此前只有发布期 ``validate_ontology`` 事后判重，入库到发布
之间无人把关。这里钉住三件事：库层真的拦得住、各写入路径给的是可行动的报错而非
IntegrityError、合并批次内部撞名在插入前就消解掉。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from app.database import Base, SessionLocal, engine
from app.models import DomainContext, EntityStatus, ObjectType, Ontology, OntologyStatus
from app.schemas import DraftObjectType
from app.services.edit import EditService
from app.services.ontology_merge import MergeReport, OntologyMergeService


@pytest.fixture(scope="module", autouse=True)
def _tables():
    Base.metadata.create_all(bind=engine)
    yield


def _fresh_ontology(db) -> Ontology:
    domain = DomainContext(datahub_domain_id=f"urn:test:{uuid.uuid4()}", name="测试域")
    db.add(domain)
    db.flush()
    ontology = Ontology(
        domain_context_id=domain.id, status=OntologyStatus.DRAFT.value, generated_by="llm"
    )
    db.add(ontology)
    db.flush()
    return ontology


def _add_object(db, ontology_id: str, name: str, display: str = "对象") -> ObjectType:
    obj = ObjectType(
        ontology_id=ontology_id,
        name=name,
        display_name=display,
        status=EntityStatus.SUGGESTED.value,
    )
    db.add(obj)
    db.flush()
    return obj


def test_duplicate_name_rejected_by_db():
    """同一本体内重名直接被库层唯一约束拦下。"""
    db = SessionLocal()
    try:
        ontology = _fresh_ontology(db)
        _add_object(db, ontology.id, "order")
        with pytest.raises(IntegrityError):
            _add_object(db, ontology.id, "order", display="另一个订单")
    finally:
        db.rollback()
        db.close()


def test_same_name_allowed_across_ontologies():
    """约束限定在本体内——不同本体可以有同名对象。"""
    db = SessionLocal()
    try:
        first = _fresh_ontology(db)
        second = _fresh_ontology(db)
        _add_object(db, first.id, "order")
        _add_object(db, second.id, "order")
        db.flush()  # 不抛即通过
    finally:
        db.rollback()
        db.close()


def test_rename_to_taken_name_raises_actionable_error():
    """改名撞车给的是「被谁占了」的提示，而不是 IntegrityError。"""
    db = SessionLocal()
    try:
        ontology = _fresh_ontology(db)
        _add_object(db, ontology.id, "order", display="订单")
        target = _add_object(db, ontology.id, "customer", display="客户")

        with pytest.raises(ValueError) as excinfo:
            EditService().update_object_type(db, target.id, name="order")
        message = str(excinfo.value)
        assert "order" in message
        assert "订单" in message  # 指名道姓说清跟谁撞了
    finally:
        db.rollback()
        db.close()


def test_rename_to_own_name_is_noop():
    """改成自己原来的名字不该被自己挡住。"""
    db = SessionLocal()
    try:
        ontology = _fresh_ontology(db)
        obj = _add_object(db, ontology.id, "order", display="订单")
        EditService().update_object_type(db, obj.id, name="order")
        assert obj.name == "order"
    finally:
        db.rollback()
        db.close()


def test_merge_allocates_name_before_insert():
    """同批次两张不同源表压成同名：插入前就消歧，不等末尾 sweep（那时已 flush 失败）。"""
    db = SessionLocal()
    try:
        ontology = _fresh_ontology(db)
        u1 = "urn:li:dataset:(urn:li:dataPlatform:mariadb,_h.tabPeriod Closing Voucher,PROD)"
        u2 = "urn:li:dataset:(urn:li:dataPlatform:mariadb,_h.tabProcess Period Closing Voucher,PROD)"
        OntologyMergeService().merge_objects(
            db,
            ontology.id,
            [
                DraftObjectType(
                    name="period_closing_voucher", display_name="期末结算凭证",
                    description=None, source_ref=u1, confidence=0.6,
                ),
                DraftObjectType(
                    name="period_closing_voucher", display_name="流程期末结算凭证",
                    description=None, source_ref=u2, confidence=0.6,
                ),
            ],
            [],
            "gen1",
            MergeReport(),
        )
        db.flush()
        names = {
            o.name
            for o in db.query(ObjectType).filter(ObjectType.ontology_id == ontology.id)
        }
        assert names == {"period_closing_voucher", "process_period_closing_voucher"}
    finally:
        db.rollback()
        db.close()


def test_manual_creation_rejects_taken_name():
    """人工建对象撞上草稿本体里已有的名字，同样给可行动报错。"""
    from app.services.manual_creation import ManualCreationService

    service = ManualCreationService()
    db = SessionLocal()
    try:
        domain = DomainContext(
            datahub_domain_id=f"urn:test:{uuid.uuid4()}", name="人工建模域"
        )
        db.add(domain)
        db.commit()

        service.create_object(
            db, domain.id, name="Loyalty Member", display_name="会员",
            description=None, properties=[],
        )
        db.commit()

        # 同一 _snake 结果（loyalty_member）→ 撞名
        with pytest.raises(ValueError) as excinfo:
            service.create_object(
                db, domain.id, name="loyalty member", display_name="会员二号",
                description=None, properties=[],
            )
        assert "loyalty_member" in str(excinfo.value)
    finally:
        db.rollback()
        db.close()


def test_merge_allocation_survives_existing_name():
    """新对象撞上**库里已有**的名字时同样在插入前让开。"""
    db = SessionLocal()
    try:
        ontology = _fresh_ontology(db)
        _add_object(db, ontology.id, "sales_invoice", display="既有发票")
        urn = "urn:li:dataset:(urn:li:dataPlatform:mariadb,_h.tabSales Invoice,PROD)"
        OntologyMergeService().merge_objects(
            db,
            ontology.id,
            [
                DraftObjectType(
                    name="sales_invoice", display_name="新发票",
                    description=None, source_ref=urn, confidence=0.6,
                )
            ],
            [],
            "gen1",
            MergeReport(),
        )
        db.flush()
        objs = db.query(ObjectType).filter(ObjectType.ontology_id == ontology.id).all()
        assert len(objs) == 2
        assert len({o.name for o in objs}) == 2
    finally:
        db.rollback()
        db.close()
