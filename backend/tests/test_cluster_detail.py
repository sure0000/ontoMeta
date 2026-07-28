"""WS3：单簇下钻接口 get_ontology_cluster_detail 单元测试。

要点（对照 grouped-graph 的宏观视图）：
- 全量成员：不受 grouped-graph 的 50 成员上限截断；
- 簇内关系边：两端都在簇内的关系被完整返回（grouped-graph 刻意丢弃了这些边）；
- 确定性：同参多次调用返回一致的成员与边；
- 未知簇 id 返回 None（路由转 404）。
"""

from __future__ import annotations

import uuid

from app.database import SessionLocal
from app.models import DomainContext, ObjectType, Ontology, OntologyStatus, RelationType
from app.services.query import OntologyQueryService

query = OntologyQueryService()


def _seed_clique_ontology(n: int = 55) -> tuple[str, int]:
    """写入一个 n 节点全连接（clique）本体，形成单个 >50 成员的聚类。返回 (ontology_id, 期望簇内边数)。"""
    suffix = uuid.uuid4().hex[:8]
    db = SessionLocal()
    try:
        domain = DomainContext(
            datahub_domain_id=f"urn:li:domain:clusterdetail-{suffix}",
            name=f"簇下钻测试域 {suffix}",
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

        objs: list[ObjectType] = []
        for i in range(n):
            obj = ObjectType(
                ontology_id=ontology.id,
                name=f"obj_{suffix}_{i:03d}",
                display_name=f"对象{i:03d}",
                status="suggested",
            )
            db.add(obj)
            objs.append(obj)
        db.flush()

        edge_count = 0
        for i in range(n):
            for j in range(i + 1, n):
                db.add(
                    RelationType(
                        ontology_id=ontology.id,
                        name=f"rel_{suffix}_{i}_{j}",
                        display_name=f"关系{i}_{j}",
                        source_object_type_id=objs[i].id,
                        target_object_type_id=objs[j].id,
                        status="suggested",
                        structure_type="foreign_key",
                    )
                )
                edge_count += 1
        db.commit()
        return ontology.id, edge_count
    finally:
        db.close()


def _dense_cluster_id(ontology_id: str, expected_size: int) -> str:
    """从 grouped-graph 里找出 node_count == expected_size 的那个聚类 id。"""
    db = SessionLocal()
    try:
        grouped = query.get_ontology_grouped_graph(db, ontology_id)
    finally:
        db.close()
    matches = [c for c in grouped.clusters if c.node_count == expected_size]
    assert matches, f"未找到 {expected_size} 成员的聚类：{[c.node_count for c in grouped.clusters]}"
    # grouped-graph 宏观视图仍受 50 成员上限截断
    cluster = matches[0]
    assert cluster.truncated is True
    assert len(cluster.nodes) == 50
    return cluster.id


def test_cluster_detail_returns_full_members_and_intra_edges(client):
    n = 55
    ontology_id, expected_edges = _seed_clique_ontology(n)
    cluster_id = _dense_cluster_id(ontology_id, n)

    db = SessionLocal()
    try:
        detail = query.get_ontology_cluster_detail(db, ontology_id, cluster_id)
    finally:
        db.close()

    assert detail is not None
    # 全量成员：不被 50 上限截断
    assert detail.node_count == n
    assert len(detail.nodes) == n
    member_ids = {node.id for node in detail.nodes}

    # 簇内边：clique 的全部 C(n,2) 条都在，且两端都在簇内
    assert len(detail.edges) == expected_edges
    for edge in detail.edges:
        assert edge.source in member_ids
        assert edge.target in member_ids
        assert edge.structure_type == "foreign_key"


def test_cluster_detail_is_deterministic(client):
    n = 55
    ontology_id, _ = _seed_clique_ontology(n)
    cluster_id = _dense_cluster_id(ontology_id, n)

    db = SessionLocal()
    try:
        first = query.get_ontology_cluster_detail(db, ontology_id, cluster_id)
        second = query.get_ontology_cluster_detail(db, ontology_id, cluster_id)
    finally:
        db.close()

    assert first is not None and second is not None
    assert [n.id for n in first.nodes] == [n.id for n in second.nodes]
    assert {e.id for e in first.edges} == {e.id for e in second.edges}


def test_cluster_detail_unknown_id_returns_none(client):
    ontology_id, _ = _seed_clique_ontology(10)
    db = SessionLocal()
    try:
        assert query.get_ontology_cluster_detail(db, ontology_id, "cluster-999") is None
    finally:
        db.close()
