"""三方合并核心：再生成保留人工修正、识别冲突、接管未编辑字段。"""

from __future__ import annotations

import json

import pytest

from app.database import Base, SessionLocal, engine
from app.models import DomainContext, EntityStatus, ObjectType, Ontology, OntologyStatus
from app.schemas import DraftObjectType, DraftProperty, DraftRelationType
from app.services.edit import _mark_overridden
from app.services.ontology_merge import MergeReport, OntologyMergeService, relation_signature


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
        domain_context_id=domain.id, status=OntologyStatus.DRAFT.value, generated_by="llm"
    )
    db.add(ontology)
    db.flush()
    return ontology


def _obj(urn: str, name: str, display: str) -> DraftObjectType:
    return DraftObjectType(
        name=name, display_name=display, description=None, source_ref=urn, confidence=0.6
    )


def test_regeneration_preserves_manual_edit_and_flags_conflict():
    merge = OntologyMergeService()
    db = SessionLocal()
    try:
        ontology = _fresh_ontology(db)
        urn = "urn:li:dataset:order"

        # 1) 首次生成
        report = MergeReport()
        merge.merge_objects(
            db, ontology.id, [_obj(urn, "order", "订单表")], [], "gen1", report
        )
        db.commit()
        obj = db.query(ObjectType).filter(ObjectType.ontology_id == ontology.id).one()
        assert obj.origin == "machine"
        assert report.to_dict()["summary"]["added"] == 1

        # 2) 人工修正 display_name
        obj.display_name = "订单"
        _mark_overridden(obj, ["display_name"])
        db.commit()
        assert obj.pinned_fields == ["display_name"]

        # 3) 再生成，机器仍产出旧的 display_name「订单表」，且改了 description（人未碰）
        report2 = MergeReport()
        incoming = _obj(urn, "order", "订单表")
        incoming.description = "订单主表"
        merge.merge_objects(db, ontology.id, [incoming], [], "gen2", report2)
        db.commit()
        db.refresh(obj)
        # 人工值被保留，机器未改该字段 → 不算冲突
        assert obj.display_name == "订单"
        # description 人没碰 → 采纳机器新值
        assert obj.description == "订单主表"

        # 4) 再生成，机器这次把 display_name 也改成了「订单信息」→ 双改冲突
        report3 = MergeReport()
        incoming2 = _obj(urn, "order", "订单信息")
        incoming2.description = "订单主表"
        merge.merge_objects(db, ontology.id, [incoming2], [], "gen3", report3)
        db.commit()
        db.refresh(obj)
        assert obj.display_name == "订单"  # 仍保留人工值
        assert obj.has_conflict
        assert obj.conflicts["display_name"]["theirs"] == "订单信息"
        assert obj.conflicts["display_name"]["ours"] == "订单"
        assert report3.to_dict()["summary"]["conflict"] == 1
    finally:
        db.rollback()
        db.close()


def test_machine_takes_over_unedited_field():
    merge = OntologyMergeService()
    db = SessionLocal()
    try:
        ontology = _fresh_ontology(db)
        urn = "urn:li:dataset:cust"
        report = MergeReport()
        merge.merge_objects(db, ontology.id, [_obj(urn, "cust", "客户表")], [], "g1", report)
        db.commit()
        obj = db.query(ObjectType).filter(ObjectType.ontology_id == ontology.id).one()

        # 未经人工编辑，机器改名 → 直接接管
        report2 = MergeReport()
        merge.merge_objects(db, ontology.id, [_obj(urn, "customer", "客户")], [], "g2", report2)
        db.commit()
        db.refresh(obj)
        assert obj.display_name == "客户"
        assert obj.name == "customer"
        assert report2.to_dict()["summary"]["updated"] == 1
    finally:
        db.rollback()
        db.close()


def test_user_created_object_is_never_touched():
    merge = OntologyMergeService()
    db = SessionLocal()
    try:
        ontology = _fresh_ontology(db)
        urn = "urn:li:dataset:manual"
        obj = ObjectType(
            ontology_id=ontology.id, name="manual", display_name="人工对象",
            source_ref=urn, status=EntityStatus.SUGGESTED.value,
            user_created=True, origin="manual",
        )
        db.add(obj)
        db.commit()

        report = MergeReport()
        merge.merge_objects(db, ontology.id, [_obj(urn, "auto", "机器名")], [], "g1", report)
        db.commit()
        db.refresh(obj)
        assert obj.display_name == "人工对象"  # 未被机器覆盖
    finally:
        db.rollback()
        db.close()


def test_relation_signature_matches_across_name_change():
    merge = OntologyMergeService()
    db = SessionLocal()
    try:
        ontology = _fresh_ontology(db)
        src = ObjectType(ontology_id=ontology.id, name="a", display_name="A",
                         source_ref="urn:a", status="suggested")
        tgt = ObjectType(ontology_id=ontology.id, name="b", display_name="B",
                         source_ref="urn:b", status="suggested")
        db.add_all([src, tgt])
        db.flush()
        resolve = {"a": src.id, "b": tgt.id}

        rel = DraftRelationType(
            name="a_to_b", display_name="属于", source_object_type_name="a",
            target_object_type_name="b", structure_type="foreign_key",
        )
        report = MergeReport()
        merge.merge_relations(db, ontology.id, [rel], lambda n: resolve.get(n), "g1", report)
        db.commit()
        stored = db.query(ObjectType).filter(ObjectType.id == src.id).one()
        assert stored is not None
        # 签名稳定
        sig = relation_signature("urn:a", "urn:b", "foreign_key")
        assert sig == "urn:a|urn:b|foreign_key"

        # 机器换了 name，但签名一致 → 命中同一条关系而非新增
        rel2 = DraftRelationType(
            name="a_belongs_b", display_name="归属", source_object_type_name="a",
            target_object_type_name="b", structure_type="foreign_key",
        )
        report2 = MergeReport()
        merge.merge_relations(db, ontology.id, [rel2], lambda n: resolve.get(n), "g2", report2)
        db.commit()
        from app.models import RelationType
        rels = db.query(RelationType).filter(RelationType.ontology_id == ontology.id).all()
        assert len(rels) == 1  # 未因改名而重复新增
        assert rels[0].display_name == "归属"
    finally:
        db.rollback()
        db.close()


def test_merge_relations_persists_mapping_object():
    """桥表塌缩关系：merge_relations 应把 mapping_object_type_name 回链为
    mapping_object_type_id（否则关系表没有承接表、发布时被丢弃、数仓无法生成列）。"""
    merge = OntologyMergeService()
    db = SessionLocal()
    try:
        ontology = _fresh_ontology(db)
        src = ObjectType(ontology_id=ontology.id, name="supplier", display_name="供应商",
                         source_ref="urn:s", status="suggested", table_role="business_object")
        tgt = ObjectType(ontology_id=ontology.id, name="company", display_name="公司",
                         source_ref="urn:c", status="suggested", table_role="business_object")
        bridge = ObjectType(ontology_id=ontology.id, name="purchase_invoice", display_name="采购发票",
                            source_ref="urn:pi", status="suggested", table_role="bridge")
        db.add_all([src, tgt, bridge])
        db.flush()
        resolve = {"supplier": src.id, "company": tgt.id, "purchase_invoice": bridge.id}

        rel = DraftRelationType(
            name="purchase_invoice", display_name="采购发票",
            source_object_type_name="supplier", target_object_type_name="company",
            structure_type="bridge_table", mapping_object_type_name="purchase_invoice",
        )
        merge.merge_relations(db, ontology.id, [rel], lambda n: resolve.get(n), "g1", MergeReport())
        db.commit()
        from app.models import RelationType
        stored = db.query(RelationType).filter(RelationType.ontology_id == ontology.id).one()
        assert stored.mapping_object_type_id == bridge.id
    finally:
        db.rollback()
        db.close()
