"""决策留痕服务层单测。

盯住四条不变量——它们各自对应一个真实的失败模式：

1. **写失败绝不影响主链路**：留痕是增强，不能让用户的确认动作报错。
2. **凭据绝不入库**：账本长期留存，是凭据泄漏的高危新出口。
3. **幂等**：用户重复点击/前端重试不该产生两条打架的记录。
4. **只记录不授权**：账本永远不能成为第二个执行授权源。
"""

from __future__ import annotations

import json

import pytest

from app.database import SessionLocal
from app.models.agent import GovernanceArtifact
from app.models.chat_bi import ChatBiConversation, ChatBiConversationTask
from app.models.chat_bi_ledger import ChatBiDecisionRecord
from app.services import chat_bi_ledger as ledger


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def conv(db):
    row = ChatBiConversation(title="决策留痕测试")
    db.add(row)
    db.commit()
    return row


def test_records_and_reads_back(db, conv):
    rid = ledger.record_decision(
        db, conversation_id=conv.id, node="requirement", summary="选了口径 A"
    )
    assert rid
    items = ledger.list_decisions(db, conv.id)
    assert [i["node"] for i in items] == ["requirement"]
    assert items[0]["summary"] == "选了口径 A"


def test_diff_drives_outcome_and_overridden_fields(db, conv):
    """人改过参数 → modified + 精确的改动字段；没改 → accepted。"""
    ledger.record_decision(
        db,
        conversation_id=conv.id,
        node="plan",
        proposed={"target_table": "a", "load_strategy": "full"},
        chosen={"target_table": "b", "load_strategy": "full"},
    )
    rec = ledger.list_decisions(db, conv.id)[-1]
    assert rec["outcome"] == "modified"
    assert rec["overridden_fields"] == ["target_table"]

    ledger.record_decision(
        db, conversation_id=conv.id, node="plan",
        proposed={"x": 1}, chosen={"x": 1},
    )
    rec2 = ledger.list_decisions(db, conv.id)[-1]
    assert rec2["outcome"] == "accepted"
    assert rec2["overridden_fields"] == []


def test_dedup_key_is_idempotent(db, conv):
    first = ledger.record_decision(
        db, conversation_id=conv.id, node="plan", dedup_key="k-same"
    )
    second = ledger.record_decision(
        db, conversation_id=conv.id, node="plan", dedup_key="k-same"
    )
    assert first == second
    assert len(ledger.list_decisions(db, conv.id)) == 1


def test_null_dedup_key_never_merges(db, conv):
    """无自然键的记录必须各记一条——可空唯一在 SQLite/MySQL 下对 NULL 不去重。"""
    ledger.record_decision(db, conversation_id=conv.id, node="data")
    ledger.record_decision(db, conversation_id=conv.id, node="data")
    assert len(ledger.list_decisions(db, conv.id)) == 2


def test_secrets_are_redacted(db, conv):
    """凭据不得入库——OnboardProposal 对用户承诺过「助手不经手也不留存」。"""
    ledger.record_decision(
        db,
        conversation_id=conv.id,
        node="data",
        chosen={
            "name": "erp",
            "password": "p@ssw0rd",
            "nested": {"access_key": "AKIA-SECRET", "host": "db.internal"},
        },
    )
    row = (
        db.query(ChatBiDecisionRecord)
        .filter(ChatBiDecisionRecord.conversation_id == conv.id)
        .order_by(ChatBiDecisionRecord.seq.desc())
        .first()
    )
    raw = row.chosen_json or ""
    assert "p@ssw0rd" not in raw
    assert "AKIA-SECRET" not in raw
    # 非敏感字段照常保留，否则留痕就没意义了
    assert "erp" in raw and "db.internal" in raw


def test_oversized_payload_is_capped(db, conv):
    """防止有人把整个结果集塞进 chosen 撑爆库。"""
    ledger.record_decision(
        db,
        conversation_id=conv.id,
        node="data",
        chosen={"rows": [{"col": "x" * 200} for _ in range(500)]},
    )
    rec = ledger.list_decisions(db, conv.id)[-1]
    assert rec["chosen"].get("_truncated") is True


def test_unknown_node_is_normalized_not_raised(db, conv):
    """上游是 UI 事件——宁可记糊一条也不要丢一条。"""
    assert ledger.record_decision(db, conversation_id=conv.id, node="bogus-node")
    assert ledger.list_decisions(db, conv.id)[-1]["node"] == "other"


def test_unknown_conversation_records_nothing_and_never_raises(db, conv):
    """**核心不变量**：留痕失败返回 None、不抛，且同一 session 仍可继续用。

    不能靠 FK 兜底——SQLite 默认不启用外键约束，孤儿行会被静默写进去。
    """
    assert ledger.record_decision(db, conversation_id="ghost", node="plan") is None
    assert (
        db.query(ChatBiDecisionRecord)
        .filter(ChatBiDecisionRecord.conversation_id == "ghost")
        .count()
        == 0
    )
    # session 未被污染，后续写入照常
    assert ledger.record_decision(db, conversation_id=conv.id, node="plan")


def test_build_closure_marks_unreached_nodes(db, conv):
    """恒六环：未到达的标灰而非隐藏——「哪一环没走」正是管理要看的。"""
    ledger.record_decision(db, conversation_id=conv.id, node="requirement")
    closure = ledger.build_closure(db, conv.id)
    assert len(closure["nodes"]) == 6
    assert closure["total_count"] == 6
    assert closure["reached_count"] == 1
    reached = {n["node"]: n["reached"] for n in closure["nodes"]}
    assert reached["requirement"] is True
    assert reached["result"] is False
    assert [n["label"] for n in closure["nodes"]][0] == "需求确认"


def test_closure_flags_confirm_without_execute(db, conv):
    ledger.record_decision(
        db, conversation_id=conv.id, node="plan", ref_kind="artifact", ref_id="art-9"
    )
    closure = ledger.build_closure(db, conv.id)
    assert any("尚未执行" in d for d in closure["dangling"])


def test_closure_flags_execute_without_result(db, conv):
    ledger.record_decision(
        db, conversation_id=conv.id, node="plan", ref_kind="artifact", ref_id="a1"
    )
    ledger.record_decision(
        db, conversation_id=conv.id, node="execute", ref_kind="artifact", ref_id="a1"
    )
    closure = ledger.build_closure(db, conv.id)
    assert any("结果尚未确认" in d for d in closure["dangling"])


def test_resolve_conversation_for_artifact(db, conv):
    db.add(ChatBiConversationTask(conversation_id=conv.id, artifact_id="art-x"))
    db.commit()
    assert ledger.resolve_conversation_for_artifact(db, "art-x") == conv.id
    # 工单直接起草的制品无会话——返回 None 是正常路径，不是错误
    assert ledger.resolve_conversation_for_artifact(db, "no-such") is None


def test_closure_carries_conversation_tasks_for_reentry(db, conv):
    """闭环必须带上本会话的任务——那是重新进入后三环的唯一入口。

    方案/执行/结果都在任务详情抽屉里确认，而抽屉此前只在「刚提交完那一下」弹一次，
    制品 id 只活在组件的 useState 里：人关掉窗口或刷新页面，这条任务就在对话里失联，
    剩下三环再也走不到。关联本来就落了库，缺的只是读回来的通道。
    """
    artifact = GovernanceArtifact(
        kind="sync", name="客户分组同步", status="validated", intent="同步客户分组"
    )
    db.add(artifact)
    db.flush()
    db.add(
        ChatBiConversationTask(
            conversation_id=conv.id, artifact_id=artifact.id, kind="sync"
        )
    )
    db.commit()

    closure = ledger.build_closure(db, conv.id)
    assert [t["artifact_id"] for t in closure["tasks"]] == [artifact.id]
    task = closure["tasks"][0]
    assert task["name"] == "客户分组同步"  # 给人认的名字，不是 uuid
    assert task["status"] == "validated"  # 前端据此决定按钮说「继续确认方案」
    assert task["kind"] == "sync"


def test_closure_tasks_survive_a_deleted_artifact(db, conv):
    """制品被删了也别在界面上摆一串 uuid——退回意图文本，至少认得出是哪件事。"""
    db.add(
        ChatBiConversationTask(
            conversation_id=conv.id, artifact_id="gone-1", kind="sync", intent="同步订单表"
        )
    )
    db.commit()
    task = ledger.build_closure(db, conv.id)["tasks"][0]
    assert task["name"] == "同步订单表" and task["status"] is None


def test_closure_tasks_dedupe_same_artifact(db, conv):
    """同一条制品可能被关联多次（重复提交/任务链推进），闭环里只该出现一行。"""
    for _ in range(2):
        db.add(ChatBiConversationTask(conversation_id=conv.id, artifact_id="dup-1"))
    db.commit()
    assert len(ledger.build_closure(db, conv.id)["tasks"]) == 1


def test_search_decisions_filters(db, conv):
    ledger.record_decision(db, conversation_id=conv.id, node="plan", ref_kind="artifact")
    ledger.record_decision(db, conversation_id=conv.id, node="data")
    assert all(r["node"] == "plan" for r in ledger.search_decisions(db, node="plan"))


def test_search_decisions_carries_conversation_title(db, conv):
    """跨会话查询必须带上会话名——追踪页只给一串 uuid 等于逼人逐条点开才知道在看什么。"""
    ledger.record_decision(db, conversation_id=conv.id, node="plan", summary="s")
    row = next(r for r in ledger.search_decisions(db) if r["conversation_id"] == conv.id)
    assert row["conversation_title"] == "决策留痕测试"


def test_closure_can_omit_records(db, conv):
    """轻量摘要不带时间线，但六环聚合照常算出来。"""
    ledger.record_decision(db, conversation_id=conv.id, node="requirement")
    light = ledger.build_closure(db, conv.id, include_records=False)
    assert light["records"] == []
    assert light["reached_count"] == 1  # 聚合本身照常算出来


def test_ack_can_be_reversed(db, conv):
    """表态可改判：先认可后存疑，闭环取最新态。

    账本是追加式的，故改判必须**追加**一条而不是改写旧的——两条都在，最新的说了算。
    这条钉住的是一个真实回归：前端曾按 (会话,环,块) 传 dedup_key，服务端命中即返回
    既有记录而不改写，于是「存疑」被静默丢弃、界面却把「存疑」点亮，账本与人看到的对不上。
    """
    common = {"conversation_id": conv.id, "node": "data", "stage": "sql", "block_id": "b3"}
    ledger.record_decision(db, **common, outcome="accepted", trigger="ack_accept")
    ledger.record_decision(db, **common, outcome="rejected", trigger="ack_doubt")

    records = [r for r in ledger.list_decisions(db, conv.id) if r["node"] == "data"]
    assert [r["outcome"] for r in records] == ["accepted", "rejected"]
    data_node = next(n for n in ledger.build_closure(db, conv.id)["nodes"] if n["node"] == "data")
    assert data_node["latest_outcome"] == "rejected"


def test_deleting_conversation_takes_its_decisions(db, conv):
    """删会话必须带走它的决策留痕。

    SQLite 默认不启外键，不显式删就会留下孤儿——在决策追踪页上表现为一行没有会话名、
    点「看闭环」还打不开的脏数据；而在真启外键的库上，删会话会被外键直接挡下变成 500。
    """
    from app.services.chat_bi import ChatBiService

    ledger.record_decision(db, conversation_id=conv.id, node="plan", summary="待删")
    assert ledger.list_decisions(db, conv.id)

    ChatBiService().delete_conversation(db, conv.id)
    assert ledger.list_decisions(db, conv.id) == []
    assert not [r for r in ledger.search_decisions(db) if r["conversation_id"] == conv.id]
