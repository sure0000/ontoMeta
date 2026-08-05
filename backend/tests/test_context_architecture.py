"""P2 上下文架构：域语义卡 + 结构化压缩。

这两件事共同顶替了 prompt 里那几段铁律——**能由构造保证的，不写进提示词**：
- 语义卡把「这个域长什么样」前移成常驻上下文（原本要模型自己调工具去问）；
- 压缩保证回灌永远是合法 JSON（原本按字符砍，几乎必然截在半个键名里）。
"""

from __future__ import annotations

import json
import uuid

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
from app.services import domain_semantic_card
from app.services.domain_semantic_card import build_card
from app.services.tool_result_compaction import compact_tool_result

PUB = EntityStatus.PUBLISHED.value


def _seed(*, n_pairs=4, n_isolated=2, n_metrics=2, draft_objects=3):
    uniq = uuid.uuid4().hex[:8]
    with SessionLocal() as db:
        domain = DomainContext(
            datahub_domain_id=f"urn:li:domain:card-{uniq}", name=f"语义卡域-{uniq}"
        )
        db.add(domain)
        db.flush()
        onto = Ontology(
            domain_context_id=domain.id, status=OntologyStatus.PUBLISHED.value,
            version=3,
        )
        db.add(onto)
        db.flush()

        for i in range(n_pairs):
            src = ObjectType(ontology_id=onto.id, name=f"fact_{i}", display_name=f"事实{i}",
                             table_role="business_object", status=PUB)
            tgt = ObjectType(ontology_id=onto.id, name=f"dim_{i}", display_name=f"维度{i}",
                             table_role="data_table", status=PUB)
            db.add_all([src, tgt])
            db.flush()
            db.add(Property(object_type_id=src.id, name="amount", display_name="金额",
                            semantic_type="measure", data_type="decimal", status=PUB))
            db.add(RelationType(
                ontology_id=onto.id, name=f"rel_{i}", display_name=f"关联{i}",
                source_object_type_id=src.id, target_object_type_id=tgt.id,
                cardinality="many_to_one", structure_type="foreign_key", status=PUB,
            ))
        for i in range(n_isolated):
            db.add(ObjectType(ontology_id=onto.id, name=f"lonely_{i}", display_name=f"孤立{i}",
                              table_role="business_object", status=PUB))
        # 未发布草稿：**不得**进入语义卡
        for i in range(draft_objects):
            db.add(ObjectType(ontology_id=onto.id, name=f"draft_{i}", display_name=f"草稿对象{i}",
                              table_role="business_object", status="edited"))
        for i in range(n_metrics):
            db.add(BusinessLogic(
                ontology_id=onto.id, name=f"metric_{i}", display_name=f"指标{i}",
                logic_type="metric", expression_summary="SUM(x)", status=PUB,
                expression_json=json.dumps({"type": "metric"}) if i == 0 else None,
            ))
        db.add(BusinessLogic(
            ontology_id=onto.id, name="draft_metric", display_name="草稿指标",
            logic_type="metric", status="edited",
        ))
        db.commit()
        return domain.id, onto.id, domain.name


def _card(onto_id: str, domain_name: str):
    domain_semantic_card.reset_cache()
    with SessionLocal() as db:
        return build_card(db, db.get(Ontology, onto_id), domain_name)


# ---------------------------------------------------------------- 语义卡


def test_card_counts_published_only(client):
    """卡上每一条都必须是 Agent 真能检索到的——草稿一律不计。"""
    _did, oid, name = _seed()
    card = _card(oid, name)

    assert card.object_count == 4 * 2 + 2          # 草稿对象不计
    assert card.relation_count == 4
    assert card.metric_count == 2                  # 草稿指标不计
    assert card.objects_with_relations == 8
    assert "草稿对象0" not in card.render()
    assert "草稿指标" not in card.render()


def test_card_reports_compilable_metric_count(client):
    """指标目录要区分「已形式化」——只有它们能被 compile_metric 编译。"""
    _did, oid, name = _seed()
    card = _card(oid, name)
    assert card.compilable_metrics == 1
    assert "1 个已形式化" in card.render()


def test_card_render_is_self_describing(client):
    """渲染文本要能独立读懂：规模、角色分布、核心对象、并声明自己只是骨架。"""
    _did, oid, name = _seed()
    text = _card(oid, name).render()

    assert "域语义卡" in text and "已发布" in text
    assert "10 个业务对象" in text and "4 条关系" in text
    assert "business_object" in text        # 角色分布
    assert "核心对象" in text
    assert "骨架" in text and "不是全集" in text


def test_card_naming_note_is_observed_not_assumed(client):
    """命名规范从真实数据观察得出，而不是假设一套写死在 prompt 里。"""
    _did, oid, name = _seed()
    assert "中文" in _card(oid, name).naming_note


def test_card_cache_keyed_by_version(client):
    """缓存键含版本：重新发布必然改变它，缓存自动失效，不依赖调用方记得清。"""
    _did, oid, name = _seed()
    first = _card(oid, name)
    with SessionLocal() as db:
        # 不清缓存，直接命中
        assert build_card(db, db.get(Ontology, oid), name) is first
        # 版本变化 → 重算
        onto = db.get(Ontology, oid)
        onto.version = 99
        db.commit()
    with SessionLocal() as db:
        again = build_card(db, db.get(Ontology, oid), name)
    assert again is not first


# ---------------------------------------------------------------- 结构化压缩


def _big_result() -> dict:
    return {
        "total_matched": 300,
        "sample": [
            {
                "id": f"o{i}", "name": f"object_{i}", "display_name": f"对象{i}",
                "description": "这是一段很长的说明文字" * 20,
                "data_type": "varchar", "semantic_type": "categorical",
                "table_role": "business_object",
            }
            for i in range(60)
        ],
    }


def test_compaction_always_yields_valid_json(client):
    """**不变式**：无论预算多小，回灌给模型的都必须是合法 JSON。

    原实现 `json.dumps(...)[:8000]` 几乎必然截在半个键名或半个中文字里，
    模型收到的是语法都不成立的片段。
    """
    for budget in (4000, 1200, 400, 120, 40):
        text, compacted = compact_tool_result(_big_result(), budget)
        parsed = json.loads(text)          # 不合法就直接抛
        assert isinstance(parsed, dict)
        assert compacted is True


def test_compaction_drops_verbose_before_structure(client):
    """降级有次序：先丢说明性长文本，实体身份（id/name）要留到最后。"""
    text, _ = compact_tool_result(_big_result(), 4000)
    parsed = json.loads(text)
    entries = parsed.get("sample") or []
    assert entries, parsed
    assert "description" not in entries[0]   # 长文本先走
    assert entries[0]["name"]                # 身份仍在


def test_compaction_marks_sampled_lists(client):
    """列表被采样时就地标注——结构自己说明自己是样本。"""
    text, _ = compact_tool_result(_big_result(), 900)
    parsed = json.loads(text)
    if "sample_is_sample" in parsed:
        assert parsed["sample_total"] == 60
        assert parsed["sample_is_sample"] is True
    else:  # 已降到摘要级
        assert parsed.get("_compacted") is True


def test_small_result_untouched(client):
    small = {"total_matched": 2, "items": [{"id": "a"}, {"id": "b"}]}
    text, compacted = compact_tool_result(small, 8000)
    assert compacted is False
    assert json.loads(text) == small
