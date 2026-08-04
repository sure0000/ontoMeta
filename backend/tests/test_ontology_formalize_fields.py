"""F1：字段枚举形式化——归一函数、清洗脚本、服务层枚举校验。"""

from __future__ import annotations

import uuid

from app.database import SessionLocal
from app.models import (
    DomainContext,
    EntityStatus,
    ObjectType,
    Ontology,
    OntologyStatus,
    Property,
    RelationType,
)
from app.ontology_types import SemanticType, normalize_cardinality, normalize_semantic_type
from app.services.edit import EditService


def _seed(db) -> tuple[str, str, str]:
    uniq = uuid.uuid4().hex[:8]
    domain = DomainContext(datahub_domain_id=f"urn:li:domain:f1-{uniq}", name=f"F1域-{uniq}")
    db.add(domain)
    db.flush()
    onto = Ontology(domain_context_id=domain.id, status=OntologyStatus.DRAFT.value)
    db.add(onto)
    db.flush()
    a = ObjectType(ontology_id=onto.id, name="order", display_name="订单",
                   table_role="business_object", status=EntityStatus.SUGGESTED.value)
    b = ObjectType(ontology_id=onto.id, name="customer", display_name="客户",
                   table_role="business_object", status=EntityStatus.SUGGESTED.value)
    db.add_all([a, b])
    db.flush()
    return onto.id, a.id, b.id


# ---------------------------------------------------------------- 归一函数

def test_normalize_cardinality_variants():
    assert normalize_cardinality("1:N").value == "one_to_many"
    assert normalize_cardinality("N:M").value == "many_to_many"
    assert normalize_cardinality("many_to_one").value == "many_to_one"
    assert normalize_cardinality("乱码") is None


def test_normalize_semantic_variants():
    assert normalize_semantic_type("amount") is SemanticType.MEASURE
    assert normalize_semantic_type("日期") is SemanticType.UNKNOWN  # 中文不在别名表
    assert normalize_semantic_type("datetime") is SemanticType.TEMPORAL
    assert normalize_semantic_type(None) is SemanticType.UNKNOWN


# ---------------------------------------------------------------- 清洗脚本

def test_formalize_script_normalizes(client):
    from scripts.formalize_ontology_fields import Report, formalize_ontology

    with SessionLocal() as db:
        onto_id, a_id, _ = _seed(db)
        db.add_all([
            Property(object_type_id=a_id, name="amount", display_name="金额",
                     semantic_type="amount", data_type="Decimal", status=EntityStatus.SUGGESTED.value),
            Property(object_type_id=a_id, name="order_id", display_name="订单号",
                     semantic_type=None, data_type="bigint", status=EntityStatus.SUGGESTED.value),
        ])
        rel = RelationType(ontology_id=onto_id, name="r", display_name="属于",
                           source_object_type_id=a_id, target_object_type_id=a_id,
                           cardinality="N:1", structure_type="foreign_key",
                           status=EntityStatus.SUGGESTED.value)
        db.add(rel)
        db.commit()

        report = Report()
        formalize_ontology(db, onto_id, report)
        db.commit()

        # amount 文本 → measure 枚举
        p_amt = db.query(Property).filter_by(object_type_id=a_id, name="amount").one()
        assert p_amt.semantic_type == "measure"
        # order_id 无语义类型 → 画像兜底为 identifier
        p_oid = db.query(Property).filter_by(object_type_id=a_id, name="order_id").one()
        assert p_oid.semantic_type == "identifier"
        # data_type 归一小写
        assert p_amt.data_type == "decimal"
        # 基数 N:1 → many_to_one
        r = db.query(RelationType).filter_by(id=rel.id).one()
        assert r.cardinality == "many_to_one"


# ---------------------------------------------------------------- 服务层枚举校验

def test_service_rejects_bad_cardinality_when_error(client, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "formal_enforcement", "error", raising=False)
    with SessionLocal() as db:
        onto_id, a_id, b_id = _seed(db)
        db.commit()
        svc = EditService()
        try:
            svc.create_relation_type(
                db, onto_id, display_name="属于",
                source_object_type_id=a_id, target_object_type_id=b_id,
                cardinality="乱码",
            )
            assert False, "应因非法基数被拒"
        except ValueError as e:
            assert "基数" in str(e)


def test_service_allows_bad_cardinality_when_warn(client, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "formal_enforcement", "warn", raising=False)
    with SessionLocal() as db:
        onto_id, a_id, b_id = _seed(db)
        db.commit()
        svc = EditService()
        # warn 模式不拦（归一在读/投影层兜底）
        out = svc.create_relation_type(
            db, onto_id, display_name="属于",
            source_object_type_id=a_id, target_object_type_id=b_id,
            cardinality="1:N",
        )
        assert out is not None


def test_service_rejects_bad_semantic_type_when_error(client, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "formal_enforcement", "error", raising=False)
    with SessionLocal() as db:
        onto_id, a_id, _ = _seed(db)
        prop = Property(object_type_id=a_id, name="f", display_name="字段",
                        semantic_type="measure", status=EntityStatus.SUGGESTED.value)
        db.add(prop)
        db.commit()
        svc = EditService()
        try:
            svc.update_property(db, prop.id, semantic_type="瞎写")
            assert False, "应因非法语义类型被拒"
        except ValueError as e:
            assert "语义类型" in str(e)
