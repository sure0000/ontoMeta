"""P1.5 语义检索：让 ILIKE 够不到的同义词也能召回。

被治的问题：`search_*` 纯 ILIKE，中文同义词（客户 / 往来单位 / Customer）一个都命不中，
只能在 prompt 里写「关键词优先用中文」这种用户级补丁。

嵌入服务在测试里用**确定性假实现**注入——被测的是索引/检索/混合排序/降级这几条链路，
不是嵌入模型的质量（那需要真模型，属 `@pytest.mark.live` 范畴）。
"""

from __future__ import annotations

import uuid

from app.database import SessionLocal
from app.models import (
    BusinessLogic,
    DomainContext,
    EntityStatus,
    ObjectType,
    Ontology,
    OntologyStatus,
    SemanticIndexEntry,
)
from app.services import semantic_search
from app.services.chat_bi import ChatBiService
from app.services.semantic_search import (
    KIND_LOGIC,
    KIND_OBJECT,
    build_index,
    merge_hits,
    search,
)

PUB = EntityStatus.PUBLISHED.value

# 用「概念 → 向量」的假嵌入：同义词映射到同一概念，于是相似度为 1。
# 这样就能在不依赖真模型的前提下，测出「往来单位召回客户」这条链路是否打通。
_CONCEPTS = {
    "客户": [1.0, 0.0, 0.0, 0.0],
    "往来单位": [1.0, 0.0, 0.0, 0.0],
    "customer": [1.0, 0.0, 0.0, 0.0],
    "订单": [0.0, 1.0, 0.0, 0.0],
    "order": [0.0, 1.0, 0.0, 0.0],
    "毛利": [0.0, 0.0, 1.0, 0.0],
    "gross_profit": [0.0, 0.0, 1.0, 0.0],
    "毛利率": [0.0, 0.0, 1.0, 0.0],
}


def _fake_embed(texts):
    out = []
    for t in texts:
        vec = [0.0, 0.0, 0.0, 0.1]  # 兜底：与任何概念都不像
        for concept, cvec in _CONCEPTS.items():
            if concept in t:
                vec = list(cvec)
                break
        out.append(vec)
    return out


def _seed() -> tuple[str, str]:
    """客户(Customer) / 订单(Order) 两个对象 + 一个「毛利」口径。"""
    uniq = uuid.uuid4().hex[:8]
    with SessionLocal() as db:
        domain = DomainContext(
            datahub_domain_id=f"urn:li:domain:sem-{uniq}", name=f"语义检索域-{uniq}"
        )
        db.add(domain)
        db.flush()
        onto = Ontology(
            domain_context_id=domain.id, status=OntologyStatus.PUBLISHED.value, version=2
        )
        db.add(onto)
        db.flush()
        db.add_all([
            ObjectType(ontology_id=onto.id, name="customer", display_name="客户",
                       table_role="business_object", status=PUB),
            ObjectType(ontology_id=onto.id, name="order", display_name="订单",
                       table_role="business_object", status=PUB),
            # 未发布草稿：不得进索引
            ObjectType(ontology_id=onto.id, name="draft_obj", display_name="草稿客户",
                       table_role="business_object", status="edited"),
        ])
        db.add(BusinessLogic(
            ontology_id=onto.id, name="gross_profit", display_name="毛利",
            logic_type="metric", expression_summary="收入 - 成本", status=PUB,
        ))
        db.commit()
        return domain.id, onto.id


def _build(onto_id: str) -> int:
    semantic_search.reset_cache()
    with SessionLocal() as db:
        return build_index(db, onto_id, embedder=_fake_embed)


# ---------------------------------------------------------------- 建索引


def test_index_covers_published_only(client):
    _did, oid = _seed()
    n = _build(oid)
    assert n == 3  # 2 个已发布对象 + 1 个已发布口径；草稿不计

    with SessionLocal() as db:
        rows = db.query(SemanticIndexEntry).filter(
            SemanticIndexEntry.ontology_id == oid
        ).all()
        assert {r.kind for r in rows} == {KIND_OBJECT, KIND_LOGIC}
        assert all(r.dim > 0 and r.vector_json for r in rows)
        assert not any("草稿" in r.text for r in rows), "未发布草稿不得进索引"


def test_rebuild_is_idempotent(client):
    """重建先清后写：不能每次发布都往里堆一份。"""
    _did, oid = _seed()
    _build(oid)
    _build(oid)
    with SessionLocal() as db:
        assert db.query(SemanticIndexEntry).filter(
            SemanticIndexEntry.ontology_id == oid
        ).count() == 3


def test_no_embedder_means_no_index_not_an_error(client):
    """未配置嵌入服务是**常态**：返回 0、不报错，检索退回 ILIKE。"""
    _did, oid = _seed()
    semantic_search.reset_cache()
    with SessionLocal() as db:
        assert build_index(db, oid, embedder=None) == 0


# ---------------------------------------------------------------- 召回


def test_synonym_recall_that_ilike_cannot_do(client):
    """**核心用例**：「往来单位」与「客户」没有一个共同字符，ILIKE 必然落空。"""
    _did, oid = _seed()
    _build(oid)
    with SessionLocal() as db:
        onto = db.get(Ontology, oid)
        hits = search(db, onto, "往来单位", kind=KIND_OBJECT, embedder=_fake_embed)
        assert hits, "同义词应能召回"
        assert "客户" in hits[0].text
        assert hits[0].score > 0.9


def test_english_query_recalls_chinese_entity(client):
    _did, oid = _seed()
    _build(oid)
    with SessionLocal() as db:
        onto = db.get(Ontology, oid)
        hits = search(db, onto, "customer", kind=KIND_OBJECT, embedder=_fake_embed)
        assert hits and "客户" in hits[0].text


def test_metric_synonym_recall(client):
    _did, oid = _seed()
    _build(oid)
    with SessionLocal() as db:
        onto = db.get(Ontology, oid)
        hits = search(db, onto, "毛利率", kind=KIND_LOGIC, embedder=_fake_embed)
        assert hits and "毛利" in hits[0].text


def test_stale_index_is_not_used(client):
    """索引按本体版本存取：版本变了（重新发布）就取不到旧索引，不会召回陈旧实体。"""
    _did, oid = _seed()
    _build(oid)
    with SessionLocal() as db:
        onto = db.get(Ontology, oid)
        onto.version = 99
        db.commit()
    semantic_search.reset_cache()
    with SessionLocal() as db:
        onto = db.get(Ontology, oid)
        assert search(db, onto, "往来单位", kind=KIND_OBJECT, embedder=_fake_embed) == []


def test_search_without_index_returns_empty(client):
    """没建索引就返回空 —— 调用方据此退回纯 ILIKE，而不是报错。"""
    _did, oid = _seed()
    semantic_search.reset_cache()
    with SessionLocal() as db:
        onto = db.get(Ontology, oid)
        assert search(db, onto, "往来单位", kind=KIND_OBJECT, embedder=_fake_embed) == []


# ---------------------------------------------------------------- 混合排序


def test_lexical_hits_always_rank_first():
    """字面命中是最强意图信号，不该被分数更高的近义实体挤掉。"""
    from app.services.semantic_search import SemanticHit

    semantic = [
        SemanticHit(kind=KIND_OBJECT, entity_id="sem1", score=0.99, text="近义"),
        SemanticHit(kind=KIND_OBJECT, entity_id="lex1", score=0.98, text="重复"),
    ]
    merged = merge_hits(["lex1", "lex2"], semantic, limit=8)
    assert merged[:2] == ["lex1", "lex2"]      # 字面在前
    assert merged[2] == "sem1"                  # 语义补后
    assert merged.count("lex1") == 1            # 去重


def test_merge_respects_limit():
    from app.services.semantic_search import SemanticHit

    semantic = [
        SemanticHit(kind=KIND_OBJECT, entity_id=f"s{i}", score=0.9, text="")
        for i in range(10)
    ]
    assert len(merge_hits(["a", "b"], semantic, limit=5)) == 5


# ---------------------------------------------------------------- 接进工具


def test_tool_augments_ilike_with_semantic(client, monkeypatch):
    """`search_objects` 应把语义召回的实体补进结果，并标注来源。"""
    did, oid = _seed()
    _build(oid)
    monkeypatch.setattr(
        semantic_search, "default_embedder", lambda _db: _fake_embed
    )

    with SessionLocal() as db:
        result, summary, is_error = ChatBiService()._dispatch_agent_tool(
            db, domain_ids=[did], ontology_ids=[oid], name="search_objects",
            args={"keyword": "往来单位"},
        )
    assert not is_error, result
    items = result.get("items") or result.get("sample") or []
    names = [i.get("display_name") for i in items]
    assert "客户" in names, f"语义召回未接进工具；实到 {names}"
    hit = next(i for i in items if i["display_name"] == "客户")
    assert hit["matched_by"] == "semantic", "语义召回须标注来源，供模型措辞时区分"


def test_tool_degrades_silently_without_index(client, monkeypatch):
    """没索引时工具照常工作——只是召回不全，绝不报错。"""
    did, oid = _seed()
    semantic_search.reset_cache()
    monkeypatch.setattr(
        semantic_search, "default_embedder", lambda _db: _fake_embed
    )
    with SessionLocal() as db:
        result, _summary, is_error = ChatBiService()._dispatch_agent_tool(
            db, domain_ids=[did], ontology_ids=[oid], name="search_objects",
            args={"keyword": "往来单位"},
        )
    assert not is_error
    assert result["items"] == [], result   # ILIKE 落空且无索引可补 → 空结果，非报错
