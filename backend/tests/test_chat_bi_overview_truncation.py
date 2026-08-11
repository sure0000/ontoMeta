"""Data Agent 概览/检索工具的「样本 ≠ 全集」契约。

复现的线上问题：问「现在有哪些已发布的本体有关系」时，agent 只拿到 8 条抽样关系
（真实命中 140 条），却在答案里写成「全部 4113 条关系覆盖了 734 个对象」——
而真实有关系的对象只有 678 个。根因是工具层静默截断：
  * search_* 只回裸列表，摘要说「命中 8 个」，模型无从知道真实总数；
  * get_domain_overview 的 objects/relations 各截 100 条却无截断标记；
  * 没有任何字段能直接回答「哪些/多少对象有关系」，模型只能靠抽样反推。

这里锁住修复后的契约：真实总数、截断标记、连通性统计。
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
from app.services.chat_bi import ChatBiService, _SEARCH_LIMIT, _search_items
from app.services.agent_grounding import FactLedger
from app.services.publish import PublishService


def _seed(name: str, *, n_linked_pairs: int, n_isolated: int) -> tuple[str, str]:
    """建 n_linked_pairs 对「源→目标」对象（各一条关系）+ n_isolated 个无关系对象，并发布。"""
    with SessionLocal() as db:
        domain = DomainContext(
            datahub_domain_id=f"urn:li:domain:{name}", name=name, description="truncation test"
        )
        db.add(domain)
        db.flush()
        ont = Ontology(domain_context_id=domain.id, status=OntologyStatus.DRAFT.value, version=0)
        db.add(ont)
        db.flush()

        for i in range(n_linked_pairs):
            src = ObjectType(
                ontology_id=ont.id, name=f"src_{i}", display_name=f"来源对象{i}",
                table_role="business_object", status="edited",
            )
            tgt = ObjectType(
                ontology_id=ont.id, name=f"tgt_{i}", display_name=f"目标对象{i}",
                table_role="business_object", status="edited",
            )
            db.add_all([src, tgt])
            db.flush()
            db.add(RelationType(
                ontology_id=ont.id, name=f"rel_{i}", display_name=f"关联关系{i}",
                source_object_type_id=src.id, target_object_type_id=tgt.id,
                status="edited",
            ))
        for i in range(n_isolated):
            db.add(ObjectType(
                ontology_id=ont.id, name=f"lonely_{i}", display_name=f"孤立对象{i}",
                table_role="business_object", status="edited",
            ))
        db.commit()
        did, oid = domain.id, ont.id

    with SessionLocal() as db:
        PublishService().publish(db, oid, operator="tester")
    return did, oid


def _overview(did: str, oid: str) -> tuple[dict, str]:
    with SessionLocal() as db:
        result, summary, is_error = ChatBiService()._dispatch_agent_tool(
            db, domain_ids=[did], ontology_ids=[oid], name="get_domain_overview", args={}
        )
    assert not is_error, result
    return result, summary


def test_overview_reports_relation_connectivity(client):
    """「哪些/多少对象有关系」必须由工具直接给出，而不是让模型从抽样反推。"""
    did, oid = _seed("连通性域", n_linked_pairs=5, n_isolated=3)
    ov, summary = _overview(did, oid)

    assert ov["published_object_count"] == 13   # 5*2 + 3
    assert ov["published_relation_count"] == 5
    # 关键：10 个对象参与了关系，3 个没有——正是原答案错报成「覆盖全部对象」的那个数
    assert ov["objects_with_relations"] == 10
    assert ov["objects_without_relations"] == 3
    assert "10 个对象有关系" in summary and "3 个无关系" in summary


def test_overview_marks_truncated_lists(client):
    """对象/关系清单超限时必须打截断标记，且 count 仍为真实总数。"""
    did, oid = _seed("截断域", n_linked_pairs=60, n_isolated=0)  # 120 对象 / 60 关系
    ov, _ = _overview(did, oid)

    assert ov["published_object_count"] == 120
    assert ov["objects_truncated"] is True
    assert ov["objects_listed"] == len(ov["objects"]) < 120
    # 关系未超限：不得误报截断
    assert ov["published_relation_count"] == 60
    assert ov["relations_truncated"] is False
    assert ov["relations_listed"] == len(ov["relations"]) == 60
    assert "truncated=true" in ov["note"]


def test_overview_objects_carry_id_so_ledger_accepts_them(client):
    """概览对象必须带 id，否则 FactLedger 静默丢弃 → 答案引用对象名被误判为幻觉。"""
    did, oid = _seed("接地域", n_linked_pairs=2, n_isolated=1)
    ov, _ = _overview(did, oid)

    assert all(o.get("id") for o in ov["objects"])
    ledger = FactLedger()
    ChatBiService._ledger_register(ledger, "get_domain_overview", ov, is_error=False)
    assert ledger.has_entity_named("来源对象0")
    assert ledger.has_entity_named("孤立对象0")


def test_most_connected_disambiguates_duplicate_display_names(client):
    """两个对象重名时只给显示名，读者会以为同一对象被列了两次。"""
    did, oid = _seed("重名域", n_linked_pairs=2, n_isolated=0)
    with SessionLocal() as db:
        objs = (
            db.query(ObjectType)
            .filter(ObjectType.ontology_id == oid, ObjectType.name.in_(["src_0", "src_1"]))
            .all()
        )
        for o in objs:
            o.display_name = "项目"
        db.commit()

    ov, _ = _overview(did, oid)
    labels = [t["display_label"] for t in ov["most_connected_objects"] if t["display_name"] == "项目"]
    assert len(labels) == 2
    assert sorted(labels) == ["项目（src_0）", "项目（src_1）"]
    # 未重名的对象保持纯显示名，不平白暴露标识符
    others = [t["display_label"] for t in ov["most_connected_objects"] if t["display_name"] != "项目"]
    assert all("（" not in lbl for lbl in others)


def test_search_reports_true_total_not_page_size(client):
    """search_* 必须回真实命中总数：原来只回 8 条裸列表，模型把「前 8 条」当成了全部。

    P2.3 起「样本 ≠ 全集」由**键名**保证：截断时字段叫 ``sample`` 而不是 ``items``，
    并附 ``sample_note`` / ``sample_facets``。原先无论截不截断都叫 ``items``，
    只能靠一整段 prompt 铁律去堵——那段铁律已随本次改造删除。
    """
    did, oid = _seed("检索域", n_linked_pairs=20, n_isolated=0)
    with SessionLocal() as db:
        result, summary, is_error = ChatBiService()._dispatch_agent_tool(
            db, domain_ids=[did], ontology_ids=[oid], name="search_relations",
            args={"keyword": "关联关系"},
        )
    assert not is_error, result
    assert result["total_matched"] == 20
    assert result["returned"] == _SEARCH_LIMIT
    assert result["truncated"] is True
    # 截断时**不得**出现 items——键名本身就在说「这只是样本」
    assert "items" not in result
    assert len(result["sample"]) == _SEARCH_LIMIT
    assert "样本" in result["sample_note"] and "20" in result["sample_note"]
    # 摘要（会展示给用户）不能再把页大小说成命中数
    assert "命中 20 个关系" in summary


def test_search_uses_items_when_complete(client):
    """未截断时键名是 items——「这就是全部命中」，与截断态形成对照。"""
    did, oid = _seed("完整检索域", n_linked_pairs=3, n_isolated=0)
    with SessionLocal() as db:
        result, summary, _ = ChatBiService()._dispatch_agent_tool(
            db, domain_ids=[did], ontology_ids=[oid], name="search_relations",
            args={"keyword": "关联关系"},
        )
    assert result["total_matched"] == 3
    assert len(result["items"]) == 3
    assert "sample" not in result and "truncated" not in result
    assert summary == "命中 3 个关系"


def test_search_envelope_still_feeds_ledger(client):
    """信封化不能切断接地：检索到的关系仍须进账本，否则答案引用它们会被误拒答。"""
    did, oid = _seed("接地检索域", n_linked_pairs=3, n_isolated=0)
    with SessionLocal() as db:
        result, _, _ = ChatBiService()._dispatch_agent_tool(
            db, domain_ids=[did], ontology_ids=[oid], name="search_relations",
            args={"keyword": "关联关系"},
        )
    ledger = FactLedger()
    ChatBiService._ledger_register(ledger, "search_relations", result, is_error=False)
    assert ledger.has_entity_named("关联关系0")
    # 兼容旧的裸列表形态
    assert _search_items([{"id": "x"}]) == [{"id": "x"}]
    assert _search_items(result) == result["items"]
