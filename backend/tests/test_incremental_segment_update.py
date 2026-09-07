"""测试增量修正：改板块归属当场生效。"""

import pytest

from app.database import Base, SessionLocal, engine
from app.models import DomainContext, ObjectType, Ontology, OntologySegment, OntologyStatus
from app.services.edit import EditService


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


def test_update_segment_assignment():
    """测试人工修改对象的板块归属，验证 overridden_fields 记录。"""
    edit = EditService()
    db = SessionLocal()
    try:
        ontology = _fresh_ontology(db)

        # 创建两个板块
        seg1 = OntologySegment(
            ontology_id=ontology.id,
            name="seg1",
            display_name="板块1",
            member_count=0,
        )
        seg2 = OntologySegment(
            ontology_id=ontology.id,
            name="seg2",
            display_name="板块2",
            member_count=0,
        )
        db.add_all([seg1, seg2])
        db.flush()

        # 创建一个对象，初始分配到 seg1
        obj = ObjectType(
            ontology_id=ontology.id,
            segment_id=seg1.id,
            name="order",
            display_name="订单",
            table_role="business_object",
        )
        db.add(obj)
        db.commit()

        # 人工改板块归属：seg1 -> seg2
        result = edit.update_object_type(
            db, obj.id, segment_id=seg2.id, operator="test_user"
        )

        # 验证板块归属已更新
        assert result.segment_id == seg2.id

        # 验证 overridden_fields 记录了人工修改
        updated_obj = db.get(ObjectType, obj.id)
        assert updated_obj is not None
        assert updated_obj.segment_id == seg2.id
        import json
        overridden = json.loads(updated_obj.overridden_fields or "[]")
        assert "segment_id" in overridden

    finally:
        db.rollback()
        db.close()


def test_move_out_of_segment():
    """测试将对象移出板块（设为 None）。"""
    edit = EditService()
    db = SessionLocal()
    try:
        ontology = _fresh_ontology(db)

        seg1 = OntologySegment(
            ontology_id=ontology.id,
            name="seg1",
            display_name="板块1",
            member_count=0,
        )
        db.add(seg1)
        db.flush()

        obj = ObjectType(
            ontology_id=ontology.id,
            segment_id=seg1.id,
            name="order",
            display_name="订单",
            table_role="business_object",
        )
        db.add(obj)
        db.commit()

        # 移出板块：传空字符串表示 None
        result = edit.update_object_type(
            db, obj.id, segment_id="", operator="test_user"
        )

        # 验证对象已移出板块
        assert result.segment_id is None

        updated_obj = db.get(ObjectType, obj.id)
        assert updated_obj is not None
        assert updated_obj.segment_id is None

    finally:
        db.rollback()
        db.close()


def test_segment_validation():
    """测试板块归属验证：板块必须存在且属于同一本体。"""
    edit = EditService()
    db = SessionLocal()
    try:
        ontology1 = _fresh_ontology(db)
        ontology2 = _fresh_ontology(db)

        seg1 = OntologySegment(
            ontology_id=ontology1.id,
            name="seg1",
            display_name="板块1",
            member_count=0,
        )
        seg2 = OntologySegment(
            ontology_id=ontology2.id,
            name="seg2",
            display_name="板块2",
            member_count=0,
        )
        db.add_all([seg1, seg2])
        db.flush()

        obj = ObjectType(
            ontology_id=ontology1.id,
            segment_id=seg1.id,
            name="order",
            display_name="订单",
            table_role="business_object",
        )
        db.add(obj)
        db.commit()

        # 尝试移动到不存在的板块
        with pytest.raises(ValueError, match="板块不存在"):
            edit.update_object_type(db, obj.id, segment_id="nonexistent")

        # 尝试移动到其他本体的板块
        with pytest.raises(ValueError, match="不能将对象移动到其他本体的板块"):
            edit.update_object_type(db, obj.id, segment_id=seg2.id)

    finally:
        db.rollback()
        db.close()


def test_create_and_rename_manual_segment():
    """人工板块可创建并重命名，且技术标识在本体内保持唯一。"""
    edit = EditService()
    db = SessionLocal()
    try:
        ontology = _fresh_ontology(db)
        created = edit.create_segment(db, ontology.id, display_name="销售管理")

        assert created.display_name == "销售管理"
        assert created.kind == "business"
        assert created.member_count == 0
        stored = db.get(OntologySegment, created.id)
        assert stored is not None and stored.user_created is True

        renamed = edit.update_segment(db, created.id, display_name="销售运营")
        assert renamed.display_name == "销售运营"
    finally:
        db.rollback()
        db.close()


def test_delete_segment_rehomes_members():
    """删除板块不会丢失成员：按既有规则重新落到剩余分区。"""
    edit = EditService()
    db = SessionLocal()
    try:
        ontology = _fresh_ontology(db)
        segment = edit.create_segment(db, ontology.id, display_name="待拆分板块")
        obj = ObjectType(
            ontology_id=ontology.id,
            segment_id=segment.id,
            name="orphan_order",
            display_name="待拆订单",
            table_role="business_object",
        )
        db.add(obj)
        db.commit()

        result = edit.delete_segment(db, segment.id)

        assert result["deleted"] is True
        assert result["reassigned"] == 1
        db.refresh(obj)
        assert obj.segment_id is not None
        target = db.get(OntologySegment, obj.segment_id)
        assert target is not None and target.kind == "system"
        deleted = db.get(OntologySegment, segment.id)
        assert deleted is not None and deleted.deleted_by_user is True
    finally:
        db.rollback()
        db.close()
