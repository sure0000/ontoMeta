"""把被误判为业务对象的事实/明细/动作表转成一条业务关系。

覆盖核心诉求：维修/清算这类「每行是一次业务事实」的表被错判成业务对象后，能一步
转成业务关系——原表作为关系的实现表（mapping_object）无损保留，降级 bridge 离开
业务对象集；非业务对象端点自动提升为 business_object（rule1）。
"""

from __future__ import annotations

from app.database import SessionLocal
from app.models import DomainContext, ObjectType, Ontology, OntologyStatus, RelationType


def _seed(tag: str, *, target_role: str = "business_object") -> dict[str, str]:
    """建 1 张事实表(equip_repair) + 2 个端点(设备/维修工)。target 端点角色可配，
    用于验证非业务对象端点会被自动提升。"""
    with SessionLocal() as db:
        domain = DomainContext(
            datahub_domain_id=f"urn:li:domain:conv-{tag}",
            name=f"conv-domain-{tag}",
            description="convert test",
        )
        db.add(domain)
        db.flush()
        ontology = Ontology(
            domain_context_id=domain.id, status=OntologyStatus.DRAFT.value, version=0
        )
        db.add(ontology)
        db.flush()
        fact = ObjectType(
            ontology_id=ontology.id,
            name="equip_repair",
            display_name="设备维修工单",
            table_role="business_object",  # 被误判
            role_reason="[待复核] 信号不足，暂按业务对象保留",
            status="suggested",
        )
        equipment = ObjectType(
            ontology_id=ontology.id,
            name="equipment",
            display_name="设备",
            table_role="business_object",
            status="suggested",
        )
        worker = ObjectType(
            ontology_id=ontology.id,
            name="worker",
            display_name="维修工",
            table_role=target_role,
            status="suggested",
        )
        db.add_all([fact, equipment, worker])
        db.commit()
        return {
            "ontology_id": ontology.id,
            "fact": fact.id,
            "equipment": equipment.id,
            "worker": worker.id,
        }


def test_convert_object_to_relation_happy_path(client, admin_headers):
    ids = _seed("happy")
    resp = client.post(
        f"/api/object-types/{ids['fact']}/convert-to-relation",
        headers=admin_headers,
        json={
            "source_object_type_id": ids["equipment"],
            "target_object_type_id": ids["worker"],
            "display_name": "维修",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # 关系：谓词=维修，端点正确，原表作为 mapping(实现表)，结构默认 fact_table。
    rel = body["relation"]
    assert rel["display_name"] == "维修"
    assert rel["source_object_type_id"] == ids["equipment"]
    assert rel["target_object_type_id"] == ids["worker"]
    assert rel["mapping_object_type_id"] == ids["fact"]
    assert rel["structure_type"] == "fact_table"

    # 原对象退役为 bridge，离开业务对象集，去向写进 role_reason，且不再待复核。
    retired = body["retired_object"]
    assert retired["table_role"] == "bridge"
    assert "已转为业务关系" in (retired["role_reason"] or "")
    assert "待复核" not in (retired["role_reason"] or "")

    # 两端本就是业务对象 → 无需提升。
    assert body["promoted_endpoints"] == []

    with SessionLocal() as db:
        obj = db.get(ObjectType, ids["fact"])
        assert obj.table_role == "bridge"
        rels = (
            db.query(RelationType)
            .filter(RelationType.mapping_object_type_id == ids["fact"])
            .all()
        )
        assert len(rels) == 1
        assert rels[0].origin == "converted"


def test_convert_promotes_non_business_endpoint(client, admin_headers):
    # target 端点是 data_table → 转换时自动提升为 business_object（rule1）。
    ids = _seed("promote", target_role="data_table")
    resp = client.post(
        f"/api/object-types/{ids['fact']}/convert-to-relation",
        headers=admin_headers,
        json={
            "source_object_type_id": ids["equipment"],
            "target_object_type_id": ids["worker"],
            "display_name": "维修",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["promoted_endpoints"] == ["维修工"]
    with SessionLocal() as db:
        assert db.get(ObjectType, ids["worker"]).table_role == "business_object"


def test_convert_rejects_same_source_and_target(client, admin_headers):
    ids = _seed("same")
    resp = client.post(
        f"/api/object-types/{ids['fact']}/convert-to-relation",
        headers=admin_headers,
        json={
            "source_object_type_id": ids["equipment"],
            "target_object_type_id": ids["equipment"],
            "display_name": "维修",
        },
    )
    assert resp.status_code == 400


def test_convert_rejects_object_as_own_endpoint(client, admin_headers):
    # 被转换的对象不能同时充当关系端点。
    ids = _seed("selfep")
    resp = client.post(
        f"/api/object-types/{ids['fact']}/convert-to-relation",
        headers=admin_headers,
        json={
            "source_object_type_id": ids["fact"],
            "target_object_type_id": ids["worker"],
            "display_name": "维修",
        },
    )
    assert resp.status_code == 400


def test_convert_missing_object_404like(client, admin_headers):
    ids = _seed("missing")
    resp = client.post(
        "/api/object-types/does-not-exist/convert-to-relation",
        headers=admin_headers,
        json={
            "source_object_type_id": ids["equipment"],
            "target_object_type_id": ids["worker"],
            "display_name": "维修",
        },
    )
    assert resp.status_code == 400
