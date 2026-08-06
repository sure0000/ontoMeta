"""阶梯式本体加载（OntologyLadderLoader）单测。

覆盖三条核心不变式：
 1. **窄而深**：命中的对象带回完整信息包（字段/关系/口径），而非骨架；
 2. **精确锁定**：明确点名的实体拿到高置信度，元问询词（字段/关系/有哪些）不抢先、不误召回；
 3. **无关即止**：与本体无关的问题不加载任何对象（stop_reason=no_candidate），不污染上下文。

用 golden 域（订单/客户，含字段+关系+口径），与其它 chat_bi 测试同源。
"""

from __future__ import annotations

from app.database import SessionLocal
from app.services.ontology_ladder import (
    OntologyLadderLoader,
    _confidence,
    _tokens,
)
from tests.test_chat_bi_golden import _seed_golden_domain


_entity_grams_probe = OntologyLadderLoader._entity_grams


# --------------------------------------------------------------------------- 分词与实体词提取


def test_tokens_uses_chinese_bigram():
    """中文按 2-gram，避免单字 ILIKE 的偶然命中（"单"命中"订单/名单"）。"""
    assert "订单" in _tokens("订单金额")
    assert "金额" in _tokens("订单金额")
    # 英文按整词
    assert "amount" in _tokens("order amount")


def test_entity_grams_deprioritize_meta_words():
    """实体词排前、元问询噪声（字段/关系/有哪些）作兜底排后。"""
    grams = _entity_grams_probe("订单有哪些字段和关系")
    assert grams[0] == "订单"  # 实体词最前
    # "字段""关系""有哪"这类含停用字的 gram 排在实体词之后
    assert grams.index("订单") < grams.index("字段")
    assert grams.index("订单") < grams.index("关系")


def test_confidence_named_entity_scores_high():
    """候选名被问题完全覆盖 → 高置信；毫不相关 → 0。"""
    q = set(_tokens("订单有哪些字段"))
    assert _confidence(q, "订单", "order", None) >= 0.5
    assert _confidence(q, "供应商", "supplier", None) == 0.0


# --------------------------------------------------------------------------- 端到端


def test_ladder_deep_loads_named_object():
    """点名"订单" → 锁定订单，带回完整信息包（字段+关系），置信度高、matched 收敛。"""
    domain_id, ontology_id, _aliases = _seed_golden_domain()
    loader = OntologyLadderLoader()
    with SessionLocal() as db:
        res = loader.load(
            db,
            domain_id=domain_id,
            ontology_id=ontology_id,
            question="订单有哪些字段和关系",
            want=1,
            with_profiles=False,
        )
    assert res.stop_reason in ("matched", "budget")
    assert len(res.objects) == 1
    order = res.objects[0]
    assert order.display_name == "订单"
    assert order.confidence >= 0.5
    # 完整信息包：字段全集（golden 域订单有 4 个字段）+ 至少一条关系
    names = {p["display_name"] for p in order.properties}
    assert {"金额", "状态"} <= names
    assert len(order.relations) >= 1


def test_ladder_stops_on_irrelevant_question():
    """与本体无关的问题 → 不加载任何对象，no_candidate 终止（不污染上下文）。"""
    domain_id, ontology_id, _aliases = _seed_golden_domain()
    loader = OntologyLadderLoader()
    with SessionLocal() as db:
        res = loader.load(
            db,
            domain_id=domain_id,
            ontology_id=ontology_id,
            question="今天天气怎么样",
            want=2,
            with_profiles=False,
        )
    assert res.objects == []
    assert res.stop_reason == "no_candidate"


def test_ladder_narrows_but_deepens():
    """want=2 时最多深加载 2 个对象——范围窄，但每个都带完整信息，非全域倾泻。"""
    domain_id, ontology_id, _aliases = _seed_golden_domain()
    loader = OntologyLadderLoader()
    with SessionLocal() as db:
        res = loader.load(
            db,
            domain_id=domain_id,
            ontology_id=ontology_id,
            question="订单和客户",
            want=2,
            with_profiles=False,
        )
    # 至多 want 个，且都带回了字段（深加载，非骨架）
    assert 1 <= len(res.objects) <= 2
    for o in res.objects:
        assert isinstance(o.properties, list)
    # 轮次轨迹可审计
    assert res.round_trace
    assert res.rounds_used >= 1


def test_ladder_result_serializes_for_context():
    """to_dict 产出可拼进上下文的结构：objects 带 confidence、note 说明用法。"""
    domain_id, ontology_id, _aliases = _seed_golden_domain()
    loader = OntologyLadderLoader()
    with SessionLocal() as db:
        res = loader.load(
            db, domain_id=domain_id, ontology_id=ontology_id,
            question="订单", want=1, with_profiles=False,
        )
    d = res.to_dict()
    assert "objects" in d and "note" in d and "stop_reason" in d
    if d["objects"]:
        assert "confidence" in d["objects"][0]
        assert "properties" in d["objects"][0]


def test_ladder_reads_local_datahub_profiling():
    """方案 A：字段有本地 DataHub profiling（sample_values/unique_count）时，
    with_profiles 直接读本地，不触源库（source=datahub_profiling）。"""
    import json

    from app.models import Property

    domain_id, ontology_id, aliases = _seed_golden_domain()
    # 给订单.状态字段写入 DataHub profiling。
    with SessionLocal() as db:
        prop = (
            db.query(Property)
            .filter(Property.object_type_id == aliases["@order"], Property.name == "status")
            .first()
        )
        assert prop is not None
        prop.sample_values_json = json.dumps(["已支付", "待支付", "已取消"], ensure_ascii=False)
        prop.unique_count = 3
        db.commit()

    loader = OntologyLadderLoader()
    with SessionLocal() as db:
        res = loader.load(
            db,
            domain_id=domain_id,
            ontology_id=ontology_id,
            question="订单的状态分布",
            want=1,
            with_profiles=True,
            # 本地 profiling 是静态元数据，不受 run_sql 权限约束——不传 role 也能读到。
        )
    assert res.objects
    order = res.objects[0]
    # 本地 profiling 被读到：来源标记 datahub_profiling，带回样例值与 distinct_count。
    local = [p for p in order.profiles if p.get("source") == "datahub_profiling"]
    assert local, f"未读到本地 profiling，profiles={order.profiles}"
    status_pf = next((p for p in local if p.get("property_name") == "status"), None)
    assert status_pf is not None
    assert status_pf["distinct_count"] == 3
    values = [tv["value"] for tv in status_pf["top_values"]]
    assert "已支付" in values
