"""数据域页面批量修改对象类型(角色)与复核状态。"""

from __future__ import annotations

from app.database import SessionLocal
from app.models import DomainContext, ObjectType, Ontology, OntologyStatus


def _seed_two_objects(tag: str) -> list[str]:
    with SessionLocal() as db:
        domain = DomainContext(
            datahub_domain_id=f"urn:li:domain:batch-{tag}",
            name=f"batch-domain-{tag}",
            description="batch test",
        )
        db.add(domain)
        db.flush()
        ontology = Ontology(
            domain_context_id=domain.id, status=OntologyStatus.DRAFT.value, version=0
        )
        db.add(ontology)
        db.flush()
        a = ObjectType(
            ontology_id=ontology.id,
            name="t_a",
            display_name="A",
            table_role="business_object",
            role_reason="[待复核] 机器判定",
            status="suggested",
        )
        b = ObjectType(
            ontology_id=ontology.id,
            name="t_b",
            display_name="B",
            table_role="business_object",
            role_reason="[待复核] 机器判定",
            status="suggested",
        )
        db.add_all([a, b])
        db.commit()
        return [a.id, b.id]


def test_batch_update_role_and_review(client, admin_headers):
    ids = _seed_two_objects("role")
    resp = client.patch(
        "/api/object-types/batch",
        headers=admin_headers,
        json={"ids": ids, "table_role": "technical"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["updated"] == 2
    with SessionLocal() as db:
        for oid in ids:
            obj = db.get(ObjectType, oid)
            assert obj.table_role == "technical"
            # 改角色即视为复核通过，[待复核] 被清除
            assert "待复核" not in (obj.role_reason or "")


def test_batch_mark_needs_review(client, admin_headers):
    ids = _seed_two_objects("review")
    # 先清掉待复核
    client.patch(
        "/api/object-types/batch",
        headers=admin_headers,
        json={"ids": ids, "needs_review": False},
    )
    resp = client.patch(
        "/api/object-types/batch",
        headers=admin_headers,
        json={"ids": ids, "needs_review": True},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["updated"] == 2
    with SessionLocal() as db:
        for oid in ids:
            assert "待复核" in (db.get(ObjectType, oid).role_reason or "")


def test_batch_rejects_invalid_role(client, admin_headers):
    ids = _seed_two_objects("invalid")
    resp = client.patch(
        "/api/object-types/batch",
        headers=admin_headers,
        json={"ids": ids, "table_role": "not_a_role"},
    )
    assert resp.status_code == 400


def test_batch_empty_ids_noop(client, admin_headers):
    resp = client.patch(
        "/api/object-types/batch",
        headers=admin_headers,
        json={"ids": [], "table_role": "technical"},
    )
    assert resp.status_code == 200
    assert resp.json()["updated"] == 0
