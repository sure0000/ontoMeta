"""会话标题自愈单测。

真实故障：侧栏里几乎每条对话都叫「新对话」。根因不是标题生成写错了，而是它
够不着——前端点「新对话」时先建了空会话，之后每次问答都带着 conversation_id
进来，走「已有会话」分支，而当初只在「无 conversation_id」分支里做了命名。

这里钉住的不变量：
1. 只要会话还是占位标题，落下首问后必须被改名（这是 bug 本身）。
2. 改名取的是**最早**那句用户提问，不是本次提问——存量老会话补名时才不会
   拿第 11 句话当标题。
3. 用户手动改过的名字绝不被覆写。
"""

from __future__ import annotations

import pytest

from app.api.deps import chat_bi_service
from app.database import SessionLocal
from app.models.chat_bi import DEFAULT_CONVERSATION_TITLE, ChatBiConversation


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
    row = ChatBiConversation(title=DEFAULT_CONVERSATION_TITLE)
    db.add(row)
    db.commit()
    return row


def test_derive_collapses_whitespace_and_truncates():
    assert (
        chat_bi_service.derive_conversation_title("  各业务域下\n订单表的行数  ")
        == "各业务域下 订单表的行数"
    )
    assert len(chat_bi_service.derive_conversation_title("问" * 200)) == 50
    assert chat_bi_service.derive_conversation_title("   ") == ""
    assert chat_bi_service.derive_conversation_title(None) == ""


def test_placeholder_conversation_gets_named_from_first_question(db, conv):
    """前端预建会话的正常路径：第一句问完，标题就不该再是「新对话」。"""
    chat_bi_service.save_message(db, conv.id, "user", "各业务域下订单表的行数分别是多少")

    title = chat_bi_service.ensure_conversation_title(
        db, conv.id, "各业务域下订单表的行数分别是多少"
    )

    assert title == "各业务域下订单表的行数分别是多少"
    assert db.get(ChatBiConversation, conv.id).title == title


def test_stale_conversation_named_from_earliest_not_latest(db, conv):
    """存量老会话补名：拿它当初的话题，不是这次随口问的那句。"""
    chat_bi_service.save_message(db, conv.id, "user", "销售订单和客户是什么关系")
    chat_bi_service.save_message(db, conv.id, "assistant", "……")
    chat_bi_service.save_message(db, conv.id, "user", "再看看库存")

    title = chat_bi_service.ensure_conversation_title(db, conv.id, "再看看库存")

    assert title == "销售订单和客户是什么关系"


def test_manual_rename_is_never_overwritten(db, conv):
    chat_bi_service.update_conversation(db, conv.id, title="我自己起的名字")
    chat_bi_service.save_message(db, conv.id, "user", "各业务域下订单表的行数")

    title = chat_bi_service.ensure_conversation_title(db, conv.id, "各业务域下订单表的行数")

    assert title == "我自己起的名字"


def test_blank_question_leaves_placeholder_intact(db, conv):
    """问句只有空白时宁可留占位，也不要把标题改成空串。"""
    chat_bi_service.save_message(db, conv.id, "user", "   ")

    title = chat_bi_service.ensure_conversation_title(db, conv.id, "   ")

    assert title == DEFAULT_CONVERSATION_TITLE


def test_missing_conversation_does_not_raise(db):
    assert (
        chat_bi_service.ensure_conversation_title(db, "no-such-id", "问题")
        == DEFAULT_CONVERSATION_TITLE
    )


def test_ask_endpoint_renames_precreated_conversation(
    client, admin_headers, db, monkeypatch
):
    """端到端复现前端那条路径：先建空会话，再带着 id 提问。

    这是 bug 的原始现场——命名分支只在「无 conversation_id」时才跑，而真实
    前端从不走那条分支。
    """
    created = client.post(
        "/api/chat-bi/conversations", headers=admin_headers, json={"domain_ids": []}
    )
    assert created.status_code == 200, created.text
    conversation_id = created.json()["id"]
    assert created.json()["title"] == DEFAULT_CONVERSATION_TITLE

    async def fake_ask(_db, **_kwargs):
        return {
            "domain_ids": [],
            "domain_names": [],
            "domain_id": None,
            "domain_name": "",
            "ontology_id": None,
            "answer": "好的。",
            "suggested_sql": None,
            "caliber_decomposition": [],
            "referenced_objects": [],
            "referenced_logics": [],
            "steps": [],
            "data_result": None,
            "ops_records": [],
            "used_mock": False,
            "_grounded": True,
            "_unverified": [],
            "skill": "ops",
        }

    monkeypatch.setattr(chat_bi_service, "ask", fake_ask)
    response = client.post(
        "/api/chat-bi/ask",
        headers=admin_headers,
        json={
            "domain_ids": [],
            "conversation_id": conversation_id,
            "question": "各业务域下订单表的行数分别是多少",
        },
    )

    assert response.status_code == 200, response.text
    # 回执里就得带上新标题，侧栏才能当场改名。
    assert response.json()["conversation_title"] == "各业务域下订单表的行数分别是多少"
    listed = client.get("/api/chat-bi/conversations", headers=admin_headers)
    titles = {c["id"]: c["title"] for c in listed.json()}
    assert titles[conversation_id] == "各业务域下订单表的行数分别是多少"
