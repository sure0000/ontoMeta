"""B7：列表分页 / 分类 GROUP BY / 图谱邻域 / SQL 次数上限。"""

from __future__ import annotations

from sqlalchemy import event

from app.database import SessionLocal, engine
from app.models import (
    BusinessLogic,
    BusinessLogicCategory,
    DomainContext,
    ObjectType,
    Ontology,
    OntologyStatus,
    RelationType,
)
from app.services.logic_query import OntologyQueryService


def _seed_large_ontology(
    *,
    n_objects: int = 120,
    n_relations: int = 150,
    n_categories: int = 0,
    logics_per_category: int = 5,
) -> tuple[str, str]:
    """写入大域测试数据，返回 (domain_id, ontology_id)。"""
    import uuid

    suffix = uuid.uuid4().hex[:8]
    db = SessionLocal()
    try:
        domain = DomainContext(
            datahub_domain_id=f"urn:li:domain:b7-{suffix}",
            name=f"B7 性能域 {suffix}",
            description="pagination/graph fixture",
        )
        db.add(domain)
        db.flush()

        ontology = Ontology(
            domain_context_id=domain.id,
            version=1,
            status=OntologyStatus.DRAFT.value,
        )
        db.add(ontology)
        db.flush()

        objects: list[ObjectType] = []
        for i in range(n_objects):
            obj = ObjectType(
                ontology_id=ontology.id,
                name=f"obj_{suffix}_{i:04d}",
                display_name=f"对象{i:04d}",
                description=f"desc {i}",
                status="suggested",
            )
            db.add(obj)
            objects.append(obj)
        db.flush()

        for i in range(n_relations):
            src = objects[i % n_objects]
            tgt = objects[(i * 3 + 1) % n_objects]
            if src.id == tgt.id:
                tgt = objects[(i + 1) % n_objects]
            db.add(
                RelationType(
                    ontology_id=ontology.id,
                    name=f"rel_{suffix}_{i:04d}",
                    display_name=f"关系{i:04d}",
                    source_object_type_id=src.id,
                    target_object_type_id=tgt.id,
                    status="suggested",
                )
            )

        if n_categories > 0:
            cats: list[BusinessLogicCategory] = []
            for i in range(n_categories):
                cat = BusinessLogicCategory(
                    name=f"分类-{suffix}-{i}",
                    description=f"cat {i}",
                )
                db.add(cat)
                cats.append(cat)
            db.flush()

            for ci, cat in enumerate(cats):
                for j in range(logics_per_category):
                    db.add(
                        BusinessLogic(
                            ontology_id=ontology.id,
                            name=f"logic_{suffix}_{ci}_{j}",
                            display_name=f"逻辑{ci}-{j}",
                            logic_type="metric",
                            status="suggested",
                            category_id=cat.id,
                        )
                    )

        db.commit()
        return domain.id, ontology.id
    finally:
        db.close()


def _count_statements(fn):
    """执行 fn() 并返回期间 SQL 语句次数。"""
    counter = {"n": 0}

    def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        counter["n"] += 1

    event.listen(engine, "before_cursor_execute", before_cursor_execute)
    try:
        result = fn()
    finally:
        event.remove(engine, "before_cursor_execute", before_cursor_execute)
    return result, counter["n"]


def test_list_object_types_pagination(client, admin_headers):
    _, ontology_id = _seed_large_ontology(n_objects=60, n_relations=80)

    page1 = client.get(
        f"/api/object-types?ontology_id={ontology_id}&limit=20&offset=0",
        headers=admin_headers,
    )
    assert page1.status_code == 200, page1.text
    body1 = page1.json()
    assert body1["total"] == 60
    assert body1["limit"] == 20
    assert body1["offset"] == 0
    assert len(body1["items"]) == 20

    page2 = client.get(
        f"/api/object-types?ontology_id={ontology_id}&limit=20&offset=20",
        headers=admin_headers,
    )
    assert page2.status_code == 200
    body2 = page2.json()
    assert len(body2["items"]) == 20
    ids1 = {it["id"] for it in body1["items"]}
    ids2 = {it["id"] for it in body2["items"]}
    assert ids1.isdisjoint(ids2)


def test_list_relation_types_pagination(client, admin_headers):
    _, ontology_id = _seed_large_ontology(n_objects=40, n_relations=55)

    res = client.get(
        f"/api/relation-types?ontology_id={ontology_id}&limit=10&offset=0",
        headers=admin_headers,
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["total"] == 55
    assert len(body["items"]) == 10


def test_business_logic_categories_group_by_query_budget(client, admin_headers):
    _seed_large_ontology(n_objects=10, n_relations=5, n_categories=12, logics_per_category=3)

    def run():
        return OntologyQueryService().list_business_logic_categories(SessionLocal())

    result, n_queries = _count_statements(run)
    assert len(result) >= 12
    # 分类列表 + 一次 GROUP BY 计数（允许连接/事务开销，但远小于 N+1）
    assert n_queries <= 6, f"分类计数 SQL 过多: {n_queries}"


def test_list_object_types_query_budget(client, admin_headers):
    _, ontology_id = _seed_large_ontology(n_objects=80, n_relations=100)

    db = SessionLocal()

    def run():
        return OntologyQueryService().list_object_types(
            db, ontology_id=ontology_id, limit=20, offset=0
        )

    page, n_queries = _count_statements(run)
    db.close()
    assert page.total == 80
    assert len(page.items) == 20
    # 期望：scope/count/page + bulk stats（属性/关系/绑定）+ domain resolve，常量级
    assert n_queries <= 20, f"对象列表 SQL 过多: {n_queries}"


def test_ontology_graph_neighborhood_not_full(client, admin_headers):
    _, ontology_id = _seed_large_ontology(n_objects=100, n_relations=120)

    res = client.get(
        f"/api/ontologies/{ontology_id}/graph?depth=1&max_nodes=40",
        headers=admin_headers,
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["total_object_count"] == 100
    assert body["total_relation_count"] == 120
    assert body["truncated"] is True
    assert len(body["nodes"]) <= 40
    assert body["center_id"]
    # 边仅连接已选节点
    node_ids = {n["id"] for n in body["nodes"]}
    for edge in body["edges"]:
        assert edge["source"] in node_ids
        assert edge["target"] in node_ids


def test_ontology_graph_expand_center(client, admin_headers):
    _, ontology_id = _seed_large_ontology(n_objects=100, n_relations=120)

    listing = client.get(
        f"/api/object-types?ontology_id={ontology_id}&limit=1",
        headers=admin_headers,
    )
    center_id = listing.json()["items"][0]["id"]

    res = client.get(
        f"/api/ontologies/{ontology_id}/graph?center_id={center_id}&depth=1&max_nodes=50",
        headers=admin_headers,
    )
    assert res.status_code == 200
    body = res.json()
    assert body["center_id"] == center_id
    assert any(n["id"] == center_id for n in body["nodes"])
    assert body["truncated"] is True


def test_external_v1_still_returns_list(client, admin_headers):
    """外部 API 契约保持 list，不受管理端 PageResult 影响。"""
    create = client.post(
        "/api/external-apps",
        headers=admin_headers,
        json={"name": "b7-ext", "description": "b7"},
    )
    assert create.status_code == 200
    api_key = create.json()["api_key"]
    app_id = create.json()["id"]

    res = client.get("/api/v1/object-types", headers={"X-API-Key": api_key})
    assert res.status_code == 200
    assert isinstance(res.json(), list)

    client.delete(f"/api/external-apps/{app_id}", headers=admin_headers)
