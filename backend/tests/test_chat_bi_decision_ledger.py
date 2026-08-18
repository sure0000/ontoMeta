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


def test_search_decisions_filters(db, conv):
    ledger.record_decision(db, conversation_id=conv.id, node="plan", ref_kind="artifact")
    ledger.record_decision(db, conversation_id=conv.id, node="data")
    assert all(r["node"] == "plan" for r in ledger.search_decisions(db, node="plan"))
