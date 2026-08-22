"""本体生命周期闭环：一域一本体 + 发布固化人工权威。

见 docs/ONTOLOGY_LIFECYCLE_REDESIGN.md。这里钉住的是「发布不再把工作台抽走」这条
不变量——发布后再生成继续合并进同一行，人工修订的三方合并基线跨发布边界连续。
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from app.database import Base, SessionLocal, engine
from app.models import (
    DomainContext,
    EntityStatus,
    ObjectType,
    Ontology,
    OntologyStatus,
    Property,
)
from app.schemas import DraftObjectType, DraftProperty, OntologyDraftOutput
from app.services import ontology_workspace
from app.services.ontology_merge import MergeReport, OntologyMergeService
from app.services.publish import PublishService


@pytest.fixture(scope="module", autouse=True)
def _tables():
    Base.metadata.create_all(bind=engine)
    yield


def _domain(db, name: str = "闭环域") -> str:
    domain = DomainContext(datahub_domain_id=f"urn:loop:{uuid.uuid4()}", name=name)
    db.add(domain)
    db.commit()
    return domain.id


def _draft_object(**kw) -> DraftObjectType:
    base = dict(
        name="sale_order",
        display_name="销售订单",
        description="机器描述",
        source_ref="urn:li:dataset:sale_order",
        confidence=0.9,
        table_role="data_table",
        role_confidence=0.8,
        role_reason="无主键",
    )
    base.update(kw)
    return DraftObjectType(**base)


def _merge_objects(db, ontology_id: str, objects, properties=None, gen_id="gen"):
    report = MergeReport()
    OntologyMergeService().merge_objects(
        db, ontology_id, objects, properties or [], gen_id, report
    )
    db.commit()
    return report


def _publish(db, ontology_id: str):
    return PublishService().publish(db, ontology_id, operator="tester")


def _pinned(entity) -> set[str]:
    return set(json.loads(entity.overridden_fields or "[]"))


def test_regenerate_after_publish_reuses_the_same_ontology_row():
    """发布后再生成不再分叉出第二行——工作本体按域取，不看 status。"""
    db = SessionLocal()
    try:
        domain_id = _domain(db)
        ontology = ontology_workspace.get_or_create_working_ontology(db, domain_id)
        db.commit()
        first_id = ontology.id

        _merge_objects(db, first_id, [_draft_object()])
        obj = db.query(ObjectType).filter(ObjectType.ontology_id == first_id).one()
        obj.table_role = "business_object"
        obj.display_name = "销售订单（人工确认）"
        obj.overridden_fields = json.dumps(["table_role", "display_name"])
        db.commit()

        _publish(db, first_id)

        again = ontology_workspace.get_or_create_working_ontology(db, domain_id)
        assert again.id == first_id
        rows = db.query(Ontology).filter(
            Ontology.domain_context_id == domain_id
        ).all()
        assert len(rows) == 1
    finally:
        db.close()


def test_publish_seeds_structural_fields_as_manual_authority():
    """发布把结构性字段钉住，描述性字段留给机器继续刷新。"""
    db = SessionLocal()
    try:
        domain_id = _domain(db)
        ontology = ontology_workspace.get_or_create_working_ontology(db, domain_id)
        db.commit()
        _merge_objects(
            db,
            ontology.id,
            [_draft_object()],
            [
                DraftProperty(
                    object_type_name="sale_order",
                    name="amt",
                    display_name="金额",
                    data_type="DECIMAL",
                    semantic_type="measure",
                    source_field_ref="amt",
                    required=False,
                    confidence=0.9,
                )
            ],
        )
        obj = db.query(ObjectType).filter(
            ObjectType.ontology_id == ontology.id
        ).one()
        obj.table_role = "business_object"
        db.commit()

        _publish(db, ontology.id)
        db.refresh(obj)

        pinned = _pinned(obj)
        assert {"name", "display_name", "table_role"} <= pinned
        assert "description" not in pinned
        assert "role_reason" not in pinned

        prop = db.query(Property).filter(Property.object_type_id == obj.id).one()
        assert {"display_name", "data_type", "semantic_type"} <= _pinned(prop)
        assert "description" not in _pinned(prop)
    finally:
        db.close()


def test_published_structural_field_conflicts_instead_of_being_overwritten():
    """已发布对象的角色被机器改判 → 值不变 + 记一条冲突。"""
    db = SessionLocal()
    try:
        domain_id = _domain(db)
        ontology = ontology_workspace.get_or_create_working_ontology(db, domain_id)
        db.commit()
        _merge_objects(db, ontology.id, [_draft_object()])
        obj = db.query(ObjectType).filter(
            ObjectType.ontology_id == ontology.id
        ).one()
        obj.table_role = "business_object"
        db.commit()
        _publish(db, ontology.id)

        _merge_objects(
            db,
            ontology.id,
            [_draft_object(table_role="bridge", role_reason="疑似桥表")],
            gen_id="gen2",
        )
        db.refresh(obj)

        assert obj.table_role == "business_object"
        conflicts = json.loads(obj.conflict_json or "{}")
        assert "table_role" in conflicts
        assert conflicts["table_role"]["theirs"] == "bridge"
    finally:
        db.close()


def test_descriptive_field_refreshes_without_conflict_after_publish():
    """描述性字段（description / role_reason）发布后仍由机器持续刷新，不进冲突面板。"""
    db = SessionLocal()
    try:
        domain_id = _domain(db)
        ontology = ontology_workspace.get_or_create_working_ontology(db, domain_id)
        db.commit()
        _merge_objects(db, ontology.id, [_draft_object()])
        obj = db.query(ObjectType).filter(
            ObjectType.ontology_id == ontology.id
        ).one()
        obj.table_role = "business_object"
        db.commit()
        _publish(db, ontology.id)

        _merge_objects(
            db,
            ontology.id,
            [_draft_object(description="更准确的机器描述")],
            gen_id="gen2",
        )
        db.refresh(obj)

        assert obj.description == "更准确的机器描述"
        assert "description" not in json.loads(obj.conflict_json or "{}")
    finally:
        db.close()


def test_publish_does_not_advance_baseline_so_conflicts_fire_once():
    """基线停在机器上次输出：机器不改口时不重复打扰人。"""
    db = SessionLocal()
    try:
        domain_id = _domain(db)
        ontology = ontology_workspace.get_or_create_working_ontology(db, domain_id)
        db.commit()
        _merge_objects(db, ontology.id, [_draft_object()])
        obj = db.query(ObjectType).filter(
            ObjectType.ontology_id == ontology.id
        ).one()
        obj.table_role = "business_object"
        db.commit()
        _publish(db, ontology.id)
        db.refresh(obj)
        assert json.loads(obj.machine_baseline)["table_role"] == "data_table"

        # 机器沿用上次结论（data_table）→ 人工值静默保留，不产生冲突。
        _merge_objects(db, ontology.id, [_draft_object()], gen_id="gen2")
        db.refresh(obj)
        assert obj.table_role == "business_object"
        assert json.loads(obj.conflict_json or "{}") == {}
    finally:
        db.close()


def test_manual_creation_lands_in_the_published_working_row():
    """人工建模写进同一行工作本体，而不是发布后再造一行空白草稿。"""
    from app.services.manual_creation import ManualCreationService, ManualPropertyInput

    db = SessionLocal()
    try:
        domain_id = _domain(db)
        ontology = ontology_workspace.get_or_create_working_ontology(db, domain_id)
        db.commit()
        _merge_objects(db, ontology.id, [_draft_object()])
        obj = db.query(ObjectType).filter(
            ObjectType.ontology_id == ontology.id
        ).one()
        obj.table_role = "business_object"
        db.commit()
        _publish(db, ontology.id)

        result = ManualCreationService().create_object(
            db,
            domain_id,
            name="manual_thing",
            display_name="人工对象",
            description=None,
            properties=[
                ManualPropertyInput(
                    name="id",
                    display_name="主键",
                    data_type="STRING",
                    semantic_type="identifier",
                    required=True,
                    primary_key=True,
                )
            ],
        )

        assert result.ontology_id == ontology.id
        assert (
            db.query(Ontology)
            .filter(Ontology.domain_context_id == domain_id)
            .count()
            == 1
        )
    finally:
        db.close()


def test_review_state_survives_role_reason_conflict_resolution():
    """复核状态与 role_reason 正交：解决「角色依据」冲突不会把对象重新打成待复核。"""
    from app.services.provenance_service import ProvenanceService

    db = SessionLocal()
    try:
        domain_id = _domain(db)
        ontology = ontology_workspace.get_or_create_working_ontology(db, domain_id)
        db.commit()
        _merge_objects(db, ontology.id, [_draft_object(needs_review=True)])
        obj = db.query(ObjectType).filter(
            ObjectType.ontology_id == ontology.id
        ).one()
        assert obj.needs_review is True

        # 人工确认：只动 needs_review，不碰 role_reason，也不钉住它。
        from app.services.edit import EditService

        EditService().update_object_type(
            db, obj.id, table_role="business_object", operator="tester"
        )
        db.refresh(obj)
        assert obj.needs_review is False
        assert "role_reason" not in _pinned(obj)

        # 机器换个措辞 → role_reason 属描述性字段，直接采纳，不产生冲突。
        _merge_objects(
            db, ontology.id, [_draft_object(role_reason="换个说法")], gen_id="gen2"
        )
        db.refresh(obj)
        assert obj.role_reason == "换个说法"
        assert obj.needs_review is False
        assert json.loads(obj.conflict_json or "{}") == {}
    finally:
        db.close()


def test_machine_never_reraises_review_flag_on_regeneration():
    """再生成不回写复核状态：人确认过的对象不会被机器重新打成待复核。"""
    db = SessionLocal()
    try:
        domain_id = _domain(db)
        ontology = ontology_workspace.get_or_create_working_ontology(db, domain_id)
        db.commit()
        _merge_objects(db, ontology.id, [_draft_object(needs_review=True)])
        obj = db.query(ObjectType).filter(
            ObjectType.ontology_id == ontology.id
        ).one()
        obj.needs_review = False
        db.commit()

        _merge_objects(
            db, ontology.id, [_draft_object(needs_review=True)], gen_id="gen2"
        )
        db.refresh(obj)
        assert obj.needs_review is False
    finally:
        db.close()


def test_database_refuses_a_second_ontology_row_per_domain():
    db = SessionLocal()
    try:
        domain_id = _domain(db)
        first = ontology_workspace.get_or_create_working_ontology(db, domain_id)
        db.commit()
        _publish(db, first.id)

        # uq_ontology_domain_context：分叉在入库这一步就被拦掉。
        # publish() 里还有一道同域 sibling 兜底，防的是没跑到这条迁移的历史库。
        db.add(
            Ontology(
                domain_context_id=domain_id,
                status=OntologyStatus.DRAFT.value,
                generated_by="rogue",
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
    finally:
        db.close()


def test_publish_preflight_matches_what_publish_does(client, admin_headers):
    """发布前自检与实际发布共用一份判定——预检说几个，发完就是几个。"""
    db = SessionLocal()
    try:
        domain_id = _domain(db, "预检域")
        ontology = ontology_workspace.get_or_create_working_ontology(db, domain_id)
        db.commit()
        _merge_objects(
            db,
            ontology.id,
            [
                _draft_object(
                    name="ok_obj", source_ref="urn:li:dataset:ok_obj",
                    table_role="business_object",
                ),
                _draft_object(
                    name="pending_obj", source_ref="urn:li:dataset:pending_obj",
                    table_role="business_object", needs_review=True,
                ),
                _draft_object(
                    name="tech_obj", source_ref="urn:li:dataset:tech_obj",
                    table_role="technical",
                ),
            ],
        )
        ontology_id = ontology.id
    finally:
        db.close()

    resp = client.get(
        f"/api/ontologies/{ontology_id}/publish-preflight", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["object_count"] == 1
    assert body["skipped_needs_review"] == 1
    assert body["skipped_non_business"] == 1
    assert body["next_version"] == 1

    db = SessionLocal()
    try:
        _publish(db, ontology_id)
        promoted = (
            db.query(ObjectType)
            .filter(
                ObjectType.ontology_id == ontology_id,
                ObjectType.status == EntityStatus.PUBLISHED.value,
            )
            .count()
        )
        assert promoted == body["object_count"]
    finally:
        db.close()


def test_domain_detail_reports_unpublished_changes(client, admin_headers):
    """A 案下人工改动已发布实体不改状态——「待固化」只能靠这个计数被看见。"""
    from app.services.edit import EditService

    db = SessionLocal()
    try:
        domain_id = _domain(db, "待固化域")
        ontology = ontology_workspace.get_or_create_working_ontology(db, domain_id)
        db.commit()
        _merge_objects(db, ontology.id, [_draft_object(table_role="business_object")])
        _publish(db, ontology.id)
        obj = db.query(ObjectType).filter(
            ObjectType.ontology_id == ontology.id
        ).one()
        obj_id = obj.id
    finally:
        db.close()

    resp = client.get(f"/api/domains/{domain_id}", headers=admin_headers)
    assert resp.json()["unpublished_change_count"] == 0
    assert resp.json()["working_ontology_status"] == "published"

    db = SessionLocal()
    try:
        EditService().update_object_type(
            db, obj_id, display_name="人工改名", operator="tester"
        )
        # A 案：已发布实体被编辑后仍是 published，立即对外生效。
        assert db.get(ObjectType, obj_id).status == EntityStatus.PUBLISHED.value
    finally:
        db.close()

    resp = client.get(f"/api/domains/{domain_id}", headers=admin_headers)
    assert resp.json()["unpublished_change_count"] == 1


def test_discard_unpublished_keeps_published_content(client, admin_headers):
    db = SessionLocal()
    try:
        domain_id = _domain(db, "丢弃域")
        ontology = ontology_workspace.get_or_create_working_ontology(db, domain_id)
        db.commit()
        _merge_objects(
            db, ontology.id,
            [_draft_object(name="keep_me", source_ref="urn:li:dataset:keep_me",
                           table_role="business_object")],
        )
        _publish(db, ontology.id)
        _merge_objects(
            db, ontology.id,
            [_draft_object(name="junk", source_ref="urn:li:dataset:junk")],
            gen_id="gen2",
        )
        ontology_id = ontology.id
        assert db.query(ObjectType).filter(
            ObjectType.ontology_id == ontology_id
        ).count() == 2
    finally:
        db.close()

    resp = client.post(
        f"/api/domains/{domain_id}/discard-unpublished", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["object_types"] == 1

    db = SessionLocal()
    try:
        remaining = db.query(ObjectType).filter(
            ObjectType.ontology_id == ontology_id
        ).all()
        assert [o.name for o in remaining] == ["keep_me"]
    finally:
        db.close()
