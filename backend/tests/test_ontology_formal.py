"""F2：发布时形式化不变式（ontology_formal）测试。

覆盖：派生无环、口径 AST 可解析、聚合语义自洽（warning）、基数良定义（warning），
以及 assert_publishable 的 error 级阻断。
"""

from __future__ import annotations

import json
import uuid

from app.database import SessionLocal
from app.models import (
    BusinessLogic,
    DomainContext,
    EntityStatus,
    ObjectType,
    Ontology,
    OntologyStatus,
    Property,
    RelationType,
)
from app.services.ontology_formal import (
    FormalValidationError,
    assert_publishable,
    check_formal_invariants,
)


def _mk_ontology(db) -> tuple[str, str, str]:
    uniq = uuid.uuid4().hex[:8]
    domain = DomainContext(datahub_domain_id=f"urn:li:domain:f2-{uniq}", name=f"F2域-{uniq}")
    db.add(domain)
    db.flush()
    onto = Ontology(domain_context_id=domain.id, status=OntologyStatus.DRAFT.value)
    db.add(onto)
    db.flush()
    a = ObjectType(ontology_id=onto.id, name="order", display_name="订单",
                   table_role="business_object", status=EntityStatus.SUGGESTED.value)
    b = ObjectType(ontology_id=onto.id, name="settle", display_name="结算表",
                   table_role="data_table", status=EntityStatus.SUGGESTED.value)
    db.add_all([a, b])
    db.flush()
    return onto.id, a.id, b.id


def test_derivation_cycle_is_error(client):
    with SessionLocal() as db:
        onto_id, a_id, b_id = _mk_ontology(db)
        # a --derivation--> b 且 b --derivation--> a：成环
        db.add_all([
            RelationType(ontology_id=onto_id, name="a_to_b", display_name="派生出",
                         source_object_type_id=a_id, target_object_type_id=b_id,
                         structure_type="derivation", status=EntityStatus.SUGGESTED.value),
            RelationType(ontology_id=onto_id, name="b_to_a", display_name="派生出",
                         source_object_type_id=b_id, target_object_type_id=a_id,
                         structure_type="derivation", status=EntityStatus.SUGGESTED.value),
        ])
        db.commit()
        issues = check_formal_invariants(db, onto_id)
    assert any(i.code == "derivation_cycle" and i.severity == "error" for i in issues)


def test_derivation_acyclic_ok(client):
    with SessionLocal() as db:
        onto_id, a_id, b_id = _mk_ontology(db)
        db.add(RelationType(ontology_id=onto_id, name="a_to_b", display_name="派生出",
                            source_object_type_id=a_id, target_object_type_id=b_id,
                            structure_type="derivation", status=EntityStatus.SUGGESTED.value))
        db.commit()
        issues = check_formal_invariants(db, onto_id)
    assert not any(i.code == "derivation_cycle" for i in issues)


def test_metric_ref_dangling_is_error(client):
    with SessionLocal() as db:
        onto_id, a_id, _ = _mk_ontology(db)
        # body 用了 r1，但 refs 为空 → 悬空引用
        expr = {"type": "metric", "refs": [],
                "body": {"operation": "sum", "args": [{"ref": "r1"}], "group_by": []}}
        db.add(BusinessLogic(ontology_id=onto_id, name="gmv", display_name="GMV",
                             logic_type="metric", expression_json=json.dumps(expr),
                             status=EntityStatus.SUGGESTED.value))
        db.commit()
        issues = check_formal_invariants(db, onto_id)
    assert any(i.code == "metric_ref_dangling" and i.severity == "error" for i in issues)


def test_metric_ref_unresolved_is_error(client):
    with SessionLocal() as db:
        onto_id, a_id, _ = _mk_ontology(db)
        expr = {"type": "metric",
                "refs": [{"ref_id": "r1", "object_type_id": "ghost-obj", "property_id": None}],
                "body": {"operation": "count", "args": [{"ref": "r1"}], "group_by": []}}
        db.add(BusinessLogic(ontology_id=onto_id, name="cnt", display_name="计数",
                             logic_type="metric", expression_json=json.dumps(expr),
                             status=EntityStatus.SUGGESTED.value))
        db.commit()
        issues = check_formal_invariants(db, onto_id)
    assert any(i.code == "metric_ref_unresolved" and i.severity == "error" for i in issues)


def test_aggregation_non_measure_is_warning(client):
    with SessionLocal() as db:
        onto_id, a_id, _ = _mk_ontology(db)
        prop = Property(object_type_id=a_id, name="status", display_name="状态",
                        semantic_type="categorical", status=EntityStatus.SUGGESTED.value)
        db.add(prop)
        db.flush()
        expr = {"type": "metric",
                "refs": [{"ref_id": "r1", "object_type_id": a_id, "property_id": prop.id}],
                "body": {"operation": "sum", "args": [{"ref": "r1"}], "group_by": []}}
        db.add(BusinessLogic(ontology_id=onto_id, name="bad", display_name="错聚合",
                             logic_type="metric", expression_json=json.dumps(expr),
                             status=EntityStatus.SUGGESTED.value))
        db.commit()
        issues = check_formal_invariants(db, onto_id)
    assert any(i.code == "aggregation_non_measure" and i.severity == "warning" for i in issues)


def test_cardinality_undefined_is_warning(client):
    with SessionLocal() as db:
        onto_id, a_id, b_id = _mk_ontology(db)
        db.add(RelationType(ontology_id=onto_id, name="fk", display_name="属于",
                            source_object_type_id=a_id, target_object_type_id=b_id,
                            structure_type="foreign_key", cardinality="乱七八糟",
                            status=EntityStatus.SUGGESTED.value))
        db.commit()
        issues = check_formal_invariants(db, onto_id)
    assert any(i.code == "cardinality_undefined" and i.severity == "warning" for i in issues)


def test_assert_publishable_blocks_on_error(client):
    with SessionLocal() as db:
        onto_id, a_id, b_id = _mk_ontology(db)
        db.add_all([
            RelationType(ontology_id=onto_id, name="a_to_b", display_name="派生出",
                         source_object_type_id=a_id, target_object_type_id=b_id,
                         structure_type="derivation", status=EntityStatus.SUGGESTED.value),
            RelationType(ontology_id=onto_id, name="b_to_a", display_name="派生出",
                         source_object_type_id=b_id, target_object_type_id=a_id,
                         structure_type="derivation", status=EntityStatus.SUGGESTED.value),
        ])
        db.commit()
        # error 模式：抛错阻断
        try:
            assert_publishable(db, onto_id, "error")
            assert False, "应抛 FormalValidationError"
        except FormalValidationError:
            pass
        # warn 模式：不抛，返回问题清单
        issues = assert_publishable(db, onto_id, "warn")
        assert any(i.code == "derivation_cycle" for i in issues)
        # off 模式：空
        assert assert_publishable(db, onto_id, "off") == []


def test_formal_validate_endpoint(client, admin_headers):
    with SessionLocal() as db:
        onto_id, a_id, b_id = _mk_ontology(db)
        db.add(RelationType(ontology_id=onto_id, name="fk", display_name="属于",
                            source_object_type_id=a_id, target_object_type_id=b_id,
                            structure_type="foreign_key", cardinality=None,
                            status=EntityStatus.SUGGESTED.value))
        db.commit()
    resp = client.get(f"/api/ontologies/{onto_id}/formal-validate", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ontology_id"] == onto_id
    assert body["ok"] is True  # 只有 warning，无 error
    assert body["warning_count"] >= 1
