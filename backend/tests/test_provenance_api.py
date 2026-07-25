"""字段级溯源 API：冲突列表 / 解决 / 钉住 / 合并报告。"""

from __future__ import annotations

import json
import uuid

from app.database import SessionLocal
from app.models import (
    DomainContext,
    DraftGenerationTask,
    ObjectType,
    Ontology,
    OntologyStatus,
)


def _seed_conflict_ontology() -> tuple[str, str]:
    db = SessionLocal()
    try:
        domain = DomainContext(datahub_domain_id=f"urn:prov:{uuid.uuid4()}", name="溯源域")
        db.add(domain)
        db.flush()
        ontology = Ontology(
            domain_context_id=domain.id, status=OntologyStatus.DRAFT.value, generated_by="llm"
        )
        db.add(ontology)
        db.flush()
        obj = ObjectType(
            ontology_id=ontology.id,
            name="order",
            display_name="订单",
            source_ref="urn:li:dataset:order",
            status="edited",
            origin="machine_edited",
            overridden_fields=json.dumps(["display_name"]),
            conflict_json=json.dumps(
                {"display_name": {"base": "订单表", "ours": "订单", "theirs": "订单信息"}}
            ),
        )
        db.add(obj)
        db.commit()
        return ontology.id, obj.id
    finally:
        db.close()


def test_list_and_resolve_conflict(client, admin_headers):
    ontology_id, obj_id = _seed_conflict_ontology()

    res = client.get(f"/api/ontologies/{ontology_id}/conflicts", headers=admin_headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["entity_type"] == "object_type"
    assert item["field"] == "display_name"
    assert item["theirs"] == "订单信息"

    # 采纳上游
    res = client.post(
        "/api/conflicts/resolve",
        headers=admin_headers,
        json={
            "entity_type": "object_type",
            "entity_id": obj_id,
            "field": "display_name",
            "resolution": "accept_theirs",
        },
    )
    assert res.status_code == 200, res.text

    db = SessionLocal()
    try:
        obj = db.get(ObjectType, obj_id)
        assert obj.display_name == "订单信息"  # 已取上游值
        assert not obj.has_conflict
        assert "display_name" not in obj.pinned_fields  # 放开
    finally:
        db.close()

    # 冲突已清空
    res = client.get(f"/api/ontologies/{ontology_id}/conflicts", headers=admin_headers)
    assert res.json()["total"] == 0


def test_pin_and_unpin_field(client, admin_headers):
    _, obj_id = _seed_conflict_ontology()

    res = client.post(
        "/api/fields/pin",
        headers=admin_headers,
        json={
            "entity_type": "object_type",
            "entity_id": obj_id,
            "field": "description",
            "pinned": True,
        },
    )
    assert res.status_code == 200, res.text
    db = SessionLocal()
    try:
        obj = db.get(ObjectType, obj_id)
        assert "description" in obj.pinned_fields
    finally:
        db.close()

    res = client.post(
        "/api/fields/pin",
        headers=admin_headers,
        json={
            "entity_type": "object_type",
            "entity_id": obj_id,
            "field": "description",
            "pinned": False,
        },
    )
    assert res.status_code == 200
    db = SessionLocal()
    try:
        obj = db.get(ObjectType, obj_id)
        assert "description" not in obj.pinned_fields
    finally:
        db.close()


def test_pin_rejects_unknown_field(client, admin_headers):
    _, obj_id = _seed_conflict_ontology()
    res = client.post(
        "/api/fields/pin",
        headers=admin_headers,
        json={
            "entity_type": "object_type",
            "entity_id": obj_id,
            "field": "not_a_field",
            "pinned": True,
        },
    )
    assert res.status_code == 400


def test_merge_report_endpoint(client, admin_headers):
    db = SessionLocal()
    try:
        domain = DomainContext(datahub_domain_id=f"urn:prov:{uuid.uuid4()}", name="报告域")
        db.add(domain)
        db.flush()
        task = DraftGenerationTask(
            domain_context_id=domain.id,
            scope="full",
            status="succeeded",
            progress=100,
            merge_report_json=json.dumps(
                {
                    "summary": {"added": 2, "updated": 1, "kept": 3, "conflict": 1, "removed": 0},
                    "object_types": {"added": [], "updated": [], "kept": [], "conflict": [], "removed": []},
                    "properties": {},
                    "relation_types": {},
                    "business_logics": {},
                }
            ),
        )
        db.add(task)
        db.commit()
        domain_id, task_id = domain.id, task.id
    finally:
        db.close()

    res = client.get(
        f"/api/domains/{domain_id}/tasks/{task_id}/merge-report", headers=admin_headers
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["summary"]["added"] == 2
    assert body["summary"]["conflict"] == 1
