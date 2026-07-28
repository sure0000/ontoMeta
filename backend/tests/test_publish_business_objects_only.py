"""数据域发布仅发布业务对象：非业务对象、关系、业务逻辑不随本体发布，
已发布浏览态/下游/计数均不展示。"""

from __future__ import annotations

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


def _seed(name: str) -> tuple[str, str, dict[str, str]]:
    """建一个含业务对象 + 数据表 + 关系 + 业务逻辑的草稿本体。返回 (domain_id, ontology_id, ids)。"""
    with SessionLocal() as db:
        domain = DomainContext(
            datahub_domain_id=f"urn:li:domain:{name}",
            name=name,
            description="publish-scope test",
        )
        db.add(domain)
        db.flush()
        ontology = Ontology(
            domain_context_id=domain.id,
            status=OntologyStatus.DRAFT.value,
            version=0,
        )
        db.add(ontology)
        db.flush()
        biz = ObjectType(
            ontology_id=ontology.id,
            name="order",
            display_name="订单",
            table_role="business_object",
            status="edited",
        )
        tech = ObjectType(
            ontology_id=ontology.id,
            name="order_audit_log",
            display_name="订单审计表",
            table_role="technical",
            status="edited",
        )
        db.add_all([biz, tech])
        db.flush()
        prop_biz = Property(
            object_type_id=biz.id, name="amount", display_name="金额", status="edited"
        )
        prop_tech = Property(
            object_type_id=tech.id, name="ts", display_name="时间", status="edited"
        )
        db.add_all([prop_biz, prop_tech])
        rel = RelationType(
            ontology_id=ontology.id,
            name="order_has_audit",
            display_name="订单审计",
            source_object_type_id=biz.id,
            target_object_type_id=tech.id,
            status="edited",
        )
        db.add(rel)
        logic = BusinessLogic(
            ontology_id=ontology.id,
            name="gmv",
            display_name="GMV",
            logic_type="metric",
            status="edited",
        )
        db.add(logic)
        db.commit()
        return (
            domain.id,
            ontology.id,
            {
                "biz": biz.id,
                "tech": tech.id,
                "prop_biz": prop_biz.id,
                "prop_tech": prop_tech.id,
                "rel": rel.id,
                "logic": logic.id,
            },
        )


def _publish(client, admin_headers, ontology_id: str) -> None:
    conf = client.post(
        "/api/confirmations",
        headers=admin_headers,
        json={
            "ontology_id": ontology_id,
            "target_type": "ontology",
            "action_type": "publish",
            "reason": "scope-test",
        },
    )
    assert conf.status_code == 200, conf.text
    ok = client.post(
        f"/api/confirmations/{conf.json()['id']}/confirm", headers=admin_headers
    )
    assert ok.status_code == 200, ok.text


def test_publish_promotes_business_objects_only(client, admin_headers):
    _, ontology_id, ids = _seed("scope-publish")
    _publish(client, admin_headers, ontology_id)

    with SessionLocal() as db:
        published = EntityStatus.PUBLISHED.value
        # 业务对象及其属性晋级为 published
        assert db.get(ObjectType, ids["biz"]).status == published
        assert db.get(Property, ids["prop_biz"]).status == published
        # 非业务对象/其属性/关系/业务逻辑未被发布晋级
        assert db.get(ObjectType, ids["tech"]).status != published
        assert db.get(Property, ids["prop_tech"]).status != published
        assert db.get(RelationType, ids["rel"]).status != published
        assert db.get(BusinessLogic, ids["logic"]).status != published


def test_published_browse_shows_business_objects_only(client, admin_headers):
    _, ontology_id, _ = _seed("scope-browse")
    _publish(client, admin_headers, ontology_id)

    objs = client.get(
        f"/api/object-types?ontology_id={ontology_id}&published_only=true",
        headers=admin_headers,
    )
    assert objs.status_code == 200
    items = objs.json()["items"]
    assert [o["name"] for o in items] == ["order"]

    rels = client.get(
        f"/api/relation-types?ontology_id={ontology_id}&published_only=true",
        headers=admin_headers,
    )
    assert rels.status_code == 200
    assert rels.json()["total"] == 0

    logics = client.get(
        f"/api/business-logics?ontology_id={ontology_id}&published_only=true",
        headers=admin_headers,
    )
    assert logics.status_code == 200
    assert logics.json()["total"] == 0


def test_published_object_count_excludes_non_business(client, admin_headers):
    domain_id, ontology_id, _ = _seed("scope-count")
    _publish(client, admin_headers, ontology_id)

    domains = client.get("/api/domains", headers=admin_headers)
    assert domains.status_code == 200
    row = next(d for d in domains.json() if d["id"] == domain_id)
    # 已发布对象计数只含业务对象（order），不含技术表。
    assert row["published_object_type_count"] == 1
