"""大本体下的规模行为：主上下文到底被灌了多少。

DATA_AGENT_V2_PLAN 里几条只在大域上成立的判断，在 2 对象的 golden 域里一条都验证不了：
步数预算不够、检索结果污染主上下文、子 agent 隔离才有价值。这里用大本体 fixture
把它们变成**可测的数**，P4.2 才有对照面——否则做完也说不清有没有用。

测的不是「答得对不对」（那是 golden set 的事），是**上下文经济学**：
定位到相关实体这件事，要往主上下文塞进去多少字符。
"""

from __future__ import annotations

import json

import pytest

from app.database import SessionLocal
from app.services.chat_bi import _TOOL_RESULT_MAX_CHARS, ChatBiService
from app.services.domain_semantic_card import build_card
from app.models import Ontology
from app.services.tool_result_compaction import compact_tool_result

from tests.fixtures.large_ontology import seed_large_ontology


@pytest.fixture(scope="module")
def large(client):
    with SessionLocal() as db:
        return seed_large_ontology(db, name_suffix="-scale")


def _tool(env, name: str, args: dict) -> tuple[dict, str, bool]:
    with SessionLocal() as db:
        return ChatBiService()._dispatch_agent_tool(
            db, domain_ids=[env.domain_id], ontology_ids=[env.ontology_id], name=name, args=args, principal_role="publisher",
        )


def _context_chars(result) -> int:
    """该工具结果回灌进主上下文后实际占用的字符数（经 P2.2 压缩）。"""
    text, _ = compact_tool_result(result, _TOOL_RESULT_MAX_CHARS)
    return len(text)


# ---------------------------------------------------------------- fixture 自身


def test_fixture_shape_is_realistic(large):
    """结构要像真实域：多板块、板块内密集、少量高连通枢纽。"""
    assert large.object_count == 6 * 12 + 3
    assert large.relation_count > large.object_count      # 关系比对象多
    # 通用字段 8 个/对象，外加每条关系在源对象上的外键列
    assert large.property_count == large.object_count * 8 + large.relation_count
    assert len(large.segments) == 6


def test_fixture_is_deterministic(client):
    """同参数必然同结果——否则基线不可对照。"""
    with SessionLocal() as db:
        a = seed_large_ontology(db, objects_per_segment=6, name_suffix="-det-a")
    with SessionLocal() as db:
        b = seed_large_ontology(db, objects_per_segment=6, name_suffix="-det-b")
    assert (a.object_count, a.relation_count) == (b.object_count, b.relation_count)
    assert a.segments == b.segments


# ---------------------------------------------------------------- 上下文规模基线


def test_overview_is_bounded_on_large_ontology(large):
    """概览必须有界：大域里它是最容易失控的一个工具。"""
    result, summary, is_error = _tool(large, "get_domain_overview", {})
    assert not is_error
    assert result["published_object_count"] == large.object_count
    chars = _context_chars(result)
    assert chars <= _TOOL_RESULT_MAX_CHARS, f"概览回灌 {chars} 字符，超出预算"


def test_broad_search_stays_bounded(large):
    """宽泛关键词（命中一大片）不得把主上下文冲垮。"""
    result, _summary, is_error = _tool(large, "search_objects", {"keyword": "销售"})
    assert not is_error
    assert result["total_matched"] > 8, "前提：这个词确实命中一大片"
    assert result["truncated"] is True
    assert _context_chars(result) < 4000


def test_locating_entities_costs_real_context(large):
    """**P4.2 的基线**：定位到目标实体，要往主上下文塞多少字符。

    模拟一次真实的检索序列——宽泛搜 → 取详情 → 再搜相邻板块。
    这些结果**全部进主上下文**，而其中绝大部分是「找的过程」，不是「找到的结论」。
    P4.2 子 agent 的全部意义就是把这段过程挪进隔离上下文，只把结论带回来。
    """
    steps = [
        ("search_objects", {"keyword": "销售"}),
        ("search_objects", {"keyword": "客户"}),
        ("search_relations", {"keyword": "归属"}),
    ]
    total = 0
    for name, args in steps:
        result, _s, is_error = _tool(large, name, args)
        assert not is_error, result
        total += _context_chars(result)

    # 取一个对象详情（大对象 8 字段 + 多条关系）
    first = _tool(large, "search_objects", {"keyword": "销售"})[0]
    obj_id = (first.get("sample") or first.get("items"))[0]["id"]
    detail, _s, _e = _tool(large, "get_object", {"object_id": obj_id})
    total += _context_chars(detail)

    # 记录基线：这就是 P4.2 要压缩的那部分。数字本身会随 fixture 规模变，
    # 断言只钉「量级确实可观」，避免变成脆弱的快照测试。
    assert total > 3000, f"检索过程占用主上下文 {total} 字符"
    print(f"\n[基线] 一次典型检索序列占用主上下文：{total} 字符 / 4 次工具调用")


def test_semantic_card_stays_small_on_large_ontology(large):
    """语义卡是**常驻**上下文，大域下更不能膨胀——它每轮都要付一次。"""
    with SessionLocal() as db:
        card = build_card(db, db.get(Ontology, large.ontology_id), "大规模域")
    text = card.render()
    assert card.object_count == large.object_count
    assert len(text) < 1200, f"语义卡 {len(text)} 字符，常驻成本过高：\n{text}"
    # 骨架该有的都在
    assert "业务板块" in text and "核心对象" in text


def test_compaction_actually_engages_on_large_results(large):
    """大域下压缩必须真的生效，且产出仍是合法 JSON。"""
    result, _s, _e = _tool(large, "get_domain_overview", {})
    raw = json.dumps(result, ensure_ascii=False, default=str)
    text, compacted = compact_tool_result(result, 2000)
    assert len(raw) > 2000, "前提：原始结果确实超预算"
    assert compacted is True
    assert len(text) <= 2000
    json.loads(text)   # 不合法就直接抛


def test_join_path_works_across_segments(large):
    """跨板块寻路：这是小域上根本测不出来的多跳场景。"""
    from app.services.ontology_projection import build_projection
    from app.services.semantic_navigator import find_join_path

    with SessionLocal() as db:
        proj = build_projection(db, large.ontology_id, None)
    src = large.segments["销售"][0]
    tgt = large.segments["物流"][0]
    paths = find_join_path(proj, src, tgt, max_hops=5)
    assert paths, f"「{src}」到「{tgt}」应能经跨板块链路连通"
    assert paths[0].hop_count >= 2, "跨板块必然多跳"
    assert paths[0].joinable
