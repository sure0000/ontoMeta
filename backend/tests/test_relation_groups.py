"""关系去重分组：按 display_name 折叠为分组列表，聚合类型/基数/置信度/复核状态。

关系 Tab 用 ``GET /api/relation-groups`` 展示去重后的 ~N 行；关系详情页用
``GET /api/relation-types?display_name=...`` 精确拉取某关系名下的全部三元组。
"""

from __future__ import annotations

from app.database import SessionLocal
from app.models import DomainContext, ObjectType, Ontology, OntologyStatus, RelationType


def _seed_ontology(name: str) -> str:
    with SessionLocal() as db:
        domain = DomainContext(
            datahub_domain_id=f"urn:li:domain:{name}",
            name=name,
            description="relation-groups test",
        )
        db.add(domain)
        db.flush()
        ontology = Ontology(
            domain_context_id=domain.id, status=OntologyStatus.DRAFT.value, version=0
        )
        db.add(ontology)
        db.commit()
        return ontology.id


def _obj(db, ontology_id: str, name: str) -> str:
    obj = ObjectType(
        ontology_id=ontology_id,
        name=name,
        display_name=name,
        table_role="business_object",
        status="edited",
    )
    db.add(obj)
    db.flush()
    return obj.id


def _seed_relations(ontology_id: str) -> None:
    with SessionLocal() as db:
        a = _obj(db, ontology_id, "a")
        b = _obj(db, ontology_id, "b")
        c = _obj(db, ontology_id, "c")
        rows = [
            # display_name「属于」×3：类型/基数一致，置信度 0.5~0.6，同一状态
            dict(display_name="属于", src=a, tgt=b, st="foreign_key", card="N:1", conf=0.5, desc="A 属于 B"),
            dict(display_name="属于", src=b, tgt=c, st="foreign_key", card="N:1", conf=0.6, desc="B 属于 C"),
            dict(display_name="属于", src=c, tgt=a, st="foreign_key", card="many_to_one", conf=0.6, desc="A 属于 B"),
            # display_name「转化」×2：类型与基数各不相同 → 聚合为多值
            dict(display_name="转化", src=a, tgt=c, st="derivation", card="1:N", conf=0.6, desc="派生"),
            dict(display_name="转化", src=c, tgt=b, st="foreign_key", card="N:1", conf=0.6, desc="引用"),
        ]
        for i, r in enumerate(rows):
            db.add(
                RelationType(
                    ontology_id=ontology_id,
                    name=f"rel_{i}",  # name 是唯一机器键，不参与去重
                    display_name=r["display_name"],
                    source_object_type_id=r["src"],
                    target_object_type_id=r["tgt"],
                    structure_type=r["st"],
                    cardinality=r["card"],
                    source_confidence=r["conf"],
                    description=r["desc"],
                    status="suggested",
                )
            )
        db.commit()


def test_relation_groups_dedup_and_aggregate(client, admin_headers):
    ontology_id = _seed_ontology("rg-agg")
    _seed_relations(ontology_id)

    resp = client.get(
        "/api/relation-groups", headers=admin_headers, params={"ontology_id": ontology_id}
    )
    assert resp.status_code == 200, resp.text
    groups = {g["display_name"]: g for g in resp.json()}

    # 5 条关系折叠为 2 个 display_name 分组；按 count 降序（属于 3 在前）
    assert set(groups) == {"属于", "转化"}
    assert [g["display_name"] for g in resp.json()] == ["属于", "转化"]

    belongs = groups["属于"]
    assert belongs["count"] == 3
    assert belongs["structure_types"] == ["foreign_key"]
    assert belongs["cardinalities"] == ["N:1"]  # many_to_one 归一化为 N:1
    assert belongs["confidence_min"] == 0.5
    assert belongs["confidence_max"] == 0.6
    assert belongs["statuses"] == ["suggested"]
    # 代表描述取组内出现最多者（"A 属于 B" 出现 2 次）
    assert belongs["description"] == "A 属于 B"

    convert = groups["转化"]
    assert convert["count"] == 2
    assert convert["structure_types"] == ["derivation", "foreign_key"]
    assert convert["cardinalities"] == ["1:N", "N:1"]


def test_relation_types_display_name_filter(client, admin_headers):
    ontology_id = _seed_ontology("rg-filter")
    _seed_relations(ontology_id)

    resp = client.get(
        "/api/relation-types",
        headers=admin_headers,
        params={"ontology_id": ontology_id, "display_name": "属于"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 3
    assert all(item["display_name"] == "属于" for item in body["items"])
