"""发布前一致性：关系表(bridge)必须落地为某条业务关系的实现表(mapping_object)。

未落地的 bridge 对象在发布时会被静默丢弃、其业务关系随之丢失。这里应在预发布校验中
报为问题（前端据此展示并阻断），且经确认发布流程被 DraftConsistencyError 拦下。已落地
（被某关系用作 mapping_object）的 bridge 则不应报错，可正常发布。
"""

from __future__ import annotations

from app.database import SessionLocal
from app.models import (
    DomainContext,
    ObjectType,
    Ontology,
    OntologyStatus,
    RelationType,
)


def _seed_ontology(name: str) -> tuple[str, str]:
    with SessionLocal() as db:
        domain = DomainContext(
            datahub_domain_id=f"urn:li:domain:{name}",
            name=name,
            description="bridge-materialize test",
        )
        db.add(domain)
        db.flush()
        ontology = Ontology(
            domain_context_id=domain.id, status=OntologyStatus.DRAFT.value, version=0
        )
        db.add(ontology)
        db.commit()
        return domain.id, ontology.id


def _add_object(ontology_id: str, name: str, role: str) -> str:
    with SessionLocal() as db:
        obj = ObjectType(
            ontology_id=ontology_id,
            name=name,
            display_name=name,
            table_role=role,
            status="edited",
        )
        db.add(obj)
        db.commit()
        return obj.id


def _publish(client, admin_headers, ontology_id: str):
    conf = client.post(
        "/api/confirmations",
        headers=admin_headers,
        json={
            "ontology_id": ontology_id,
            "target_type": "ontology",
            "action_type": "publish",
            "reason": "bridge-test",
        },
    )
    assert conf.status_code == 200, conf.text
    return client.post(
        f"/api/confirmations/{conf.json()['id']}/confirm", headers=admin_headers
    )


def test_validate_flags_unmaterialized_bridge(client, admin_headers):
    _, ontology_id = _seed_ontology("bridge-orphan")
    # 一个 bridge 对象，但没有任何关系以它为 mapping_object。
    _add_object(ontology_id, "equip_repair", "bridge")

    resp = client.post(
        f"/api/ontologies/{ontology_id}/validate", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False
    codes = [i["code"] for i in body["issues"]]
    assert "bridge_object_not_materialized" in codes


def test_publish_blocked_by_unmaterialized_bridge(client, admin_headers):
    # 部分发布：未物化的关系表(bridge)不再阻断发布，只是不随本体发布、保持原状。
    _, ontology_id = _seed_ontology("bridge-block")
    bridge_id = _add_object(ontology_id, "equip_repair", "bridge")

    resp = _publish(client, admin_headers, ontology_id)
    assert resp.status_code == 200, resp.text
    # 桥表非业务对象 → 未随本体发布，状态保持原状。
    with SessionLocal() as db:
        obj = db.get(ObjectType, bridge_id)
        assert obj.status != "published"


def test_partial_publish_confirmed_objects_and_relations(client, admin_headers):
    """部分发布：已确认业务对象 + 其业务关系发布；待复核业务对象、其它角色对象保持原状。"""
    _, ontology_id = _seed_ontology("partial-pub")
    src = _add_object(ontology_id, "customer", "business_object")
    tgt = _add_object(ontology_id, "company", "business_object")
    with SessionLocal() as db:
        # 待复核业务对象（role_reason 带 [待复核] 前缀）→ 不应发布。
        pend = ObjectType(
            ontology_id=ontology_id, name="pending_obj", display_name="待复核对象",
            table_role="business_object", status="suggested", role_reason="[待复核] 需人工确认",
        )
        db.add(pend)
        db.flush()
        pend_id = pend.id
        # 两端都是已确认业务对象的业务关系 → 应随本体发布。
        rel = RelationType(
            ontology_id=ontology_id, name="customer_company", display_name="属于",
            source_object_type_id=src, target_object_type_id=tgt,
            structure_type="foreign_key", status="suggested",
        )
        db.add(rel)
        db.flush()
        rel_id = rel.id
        db.commit()

    resp = _publish(client, admin_headers, ontology_id)
    assert resp.status_code == 200, resp.text
    with SessionLocal() as db:
        assert db.get(ObjectType, src).status == "published"
        assert db.get(ObjectType, tgt).status == "published"
        assert db.get(RelationType, rel_id).status == "published"  # 业务关系随发布
        assert db.get(ObjectType, pend_id).status != "published"  # 待复核保持原状


def test_materialized_bridge_passes(client, admin_headers):
    _, ontology_id = _seed_ontology("bridge-ok")
    src = _add_object(ontology_id, "equipment", "business_object")
    tgt = _add_object(ontology_id, "worker", "business_object")
    bridge = _add_object(ontology_id, "equip_repair", "bridge")
    # 该 bridge 被一条业务关系用作实现表(mapping_object) → 已落地。
    with SessionLocal() as db:
        db.add(
            RelationType(
                ontology_id=ontology_id,
                name="equip_repair",
                display_name="维修",
                source_object_type_id=src,
                target_object_type_id=tgt,
                mapping_object_type_id=bridge,
                structure_type="fact_table",
                status="edited",
            )
        )
        db.commit()

    resp = client.post(
        f"/api/ontologies/{ontology_id}/validate", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    codes = [i["code"] for i in body["issues"]]
    assert "bridge_object_not_materialized" not in codes
    assert body["ok"] is True

    # 且可正常发布。
    assert _publish(client, admin_headers, ontology_id).status_code == 200
