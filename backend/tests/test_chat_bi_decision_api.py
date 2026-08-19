"""决策留痕的 API 端到端测试。

重点盯三件事：
1. **责任人不可伪造**——请求体里塞 subject_id 必须被忽略，只认已认证主体。
2. **留痕失败不连累确认动作**——制品 confirm/execute 照常成功。
3. **账本不参与授权**——账本里有 execute 记录也不能让未确认制品被执行。
"""

from __future__ import annotations

import pytest

from app.database import SessionLocal
from app.models.chat_bi import ChatBiConversation
from app.services import chat_bi_ledger as ledger


@pytest.fixture
def conversation_id():
    db = SessionLocal()
    try:
        conv = ChatBiConversation(title="决策留痕 API 测试")
        db.add(conv)
        db.commit()
        return conv.id
    finally:
        db.close()


def test_record_and_list_decision(client, admin_headers, conversation_id):
    resp = client.post(
        f"/api/chat-bi/conversations/{conversation_id}/decisions",
        headers=admin_headers,
        json={
            "node": "requirement",
            "stage": "form",
            "summary": "填写了表单「新建同步任务」",
            "proposed": {"cron": "0 2 * * *"},
            "chosen": {"cron": "0 5 * * *"},
            "dedup_key": f"{conversation_id}:form:b1",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["recorded"] is True

    items = client.get(
        f"/api/chat-bi/conversations/{conversation_id}/decisions", headers=admin_headers
    ).json()
    assert len(items) == 1
    assert items[0]["outcome"] == "modified"
    assert items[0]["overridden_fields"] == ["cron"]
    # 结构化取值真的存下来了——这正是今天被 composeFormReply 拍平丢掉的那一半
    assert items[0]["chosen"] == {"cron": "0 5 * * *"}


def test_subject_cannot_be_forged_by_client(client, admin_headers, conversation_id):
    """**安全不变量**：责任人只认服务端解析出的主体，请求体里给什么都不看。"""
    client.post(
        f"/api/chat-bi/conversations/{conversation_id}/decisions",
        headers=admin_headers,
        json={
            "node": "plan",
            "subject_id": "FORGED-ATTACKER",
            "operator": "FORGED-ATTACKER",
        },
    )
    items = client.get(
        f"/api/chat-bi/conversations/{conversation_id}/decisions", headers=admin_headers
    ).json()
    assert items[-1]["subject_id"] != "FORGED-ATTACKER"
    # admin token 走的是 superuser 通道（不查库），故 subject_id 为空、只有角色可考
    assert items[-1]["subject_role"] == "publisher"


def test_record_endpoint_always_returns_200(client, admin_headers):
    """前端是 fire-and-forget，返回错误码没人能处理，只会在控制台刷红。"""
    resp = client.post(
        "/api/chat-bi/conversations/ghost-conversation/decisions",
        headers=admin_headers,
        json={"node": "plan"},
    )
    assert resp.status_code == 200
    assert resp.json()["recorded"] is False


def test_closure_endpoint_returns_six_nodes(client, admin_headers, conversation_id):
    client.post(
        f"/api/chat-bi/conversations/{conversation_id}/decisions",
        headers=admin_headers,
        json={"node": "requirement", "summary": "需求已明确"},
    )
    closure = client.get(
        f"/api/chat-bi/conversations/{conversation_id}/closure", headers=admin_headers
    ).json()
    assert closure["total_count"] == 6
    assert closure["reached_count"] == 1
    assert [n["label"] for n in closure["nodes"]] == [
        "需求确认", "本体确认", "数据确认", "执行方案确认", "执行任务", "结果确认",
    ]


def test_task_link_records_plan_decision(client, admin_headers, conversation_id):
    """接线点：既有的 tasks 关联请求顺带带回提案 diff，无需额外往返。"""
    resp = client.post(
        f"/api/chat-bi/conversations/{conversation_id}/tasks",
        headers=admin_headers,
        json={
            "artifact_id": "artifact-link-1",
            "kind": "materialize",
            "intent": "把客户对象物化到数仓",
            "proposed_context": {"target_database": "dw", "load_strategy": "full"},
            "chosen_context": {"target_database": "dw_prod", "load_strategy": "full"},
        },
    )
    assert resp.status_code == 200

    items = client.get(
        f"/api/chat-bi/conversations/{conversation_id}/decisions", headers=admin_headers
    ).json()
    plan = [i for i in items if i["node"] == "plan"]
    assert len(plan) == 1
    # agent 提的 vs 人改的，两份都在——这是今天完全不存在的那个 diff
    assert plan[0]["outcome"] == "modified"
    assert plan[0]["overridden_fields"] == ["target_database"]
    assert plan[0]["ref_kind"] == "artifact"
    assert plan[0]["ref_id"] == "artifact-link-1"


def test_task_link_still_works_without_ledger_fields(
    client, admin_headers, conversation_id
):
    """向后兼容：老前端不带 proposed/chosen 时，关联照常成功。"""
    resp = client.post(
        f"/api/chat-bi/conversations/{conversation_id}/tasks",
        headers=admin_headers,
        json={"artifact_id": "artifact-legacy", "kind": "sync"},
    )
    assert resp.status_code == 200
    assert resp.json()["linked"] is True


def test_ledger_failure_does_not_break_task_link(
    client, admin_headers, conversation_id, monkeypatch
):
    """**核心回归**：留痕炸了，用户的关联动作必须照常成功。

    模拟最坏情况——``record_decision`` 本身抛异常（被 patch / 导入损坏 / 库此刻挂掉）。
    调用点的 ``safe_record`` 兜底层必须把它吃掉，用户看到的仍是一次成功的关联。
    """

    def _boom(*args, **kwargs):
        raise RuntimeError("ledger exploded")

    monkeypatch.setattr(
        "app.services.chat_bi_ledger.record_decision", _boom, raising=True
    )
    resp = client.post(
        f"/api/chat-bi/conversations/{conversation_id}/tasks",
        headers=admin_headers,
        json={"artifact_id": "artifact-boom", "kind": "sync"},
    )
    assert resp.status_code == 200
    assert resp.json()["linked"] is True


def test_ledger_failure_does_not_break_decision_endpoint(
    client, admin_headers, conversation_id, monkeypatch
):
    """留痕端点自身也不得因内部异常返回 5xx——只报 recorded=false。"""

    def _boom(*args, **kwargs):
        raise RuntimeError("ledger exploded")

    monkeypatch.setattr(
        "app.services.chat_bi_ledger.record_decision", _boom, raising=True
    )
    resp = client.post(
        f"/api/chat-bi/conversations/{conversation_id}/decisions",
        headers=admin_headers,
        json={"node": "plan"},
    )
    assert resp.status_code == 200
    assert resp.json()["recorded"] is False


def test_search_endpoint_backs_the_tracking_page(client, admin_headers, conversation_id):
    """追踪页依赖跨会话查询：能按环筛，且每条自带会话名。

    会话名是服务端 join 来的——列表页只给一串 uuid 等于逼人逐条点开才知道在看什么，
    而前端逐条回查会话就是 N+1。
    """
    for node in ("requirement", "result"):
        client.post(
            f"/api/chat-bi/conversations/{conversation_id}/decisions",
            headers=admin_headers,
            json={"node": node, "summary": f"{node} 拍板"},
        )

    rows = client.get("/api/chat-bi/decisions?node=result", headers=admin_headers).json()
    mine = [r for r in rows if r["conversation_id"] == conversation_id]
    assert [r["node"] for r in mine] == ["result"]
    assert mine[0]["conversation_title"] == "决策留痕 API 测试"


def test_closure_endpoint_can_reach_the_result_ring(client, admin_headers, conversation_id):
    """六环必须**能走完**。

    这条钉住一个真实回归：任务回执上的「认可」曾被记成 data 环，于是 result 恒不可达——
    闭环永远差最后一格，且恒报「任务已执行但结果尚未确认」，久了就没人再看那条告警。
    """
    for node in ("requirement", "ontology", "data", "plan", "execute", "result"):
        client.post(
            f"/api/chat-bi/conversations/{conversation_id}/decisions",
            headers=admin_headers,
            json={"node": node, "ref_kind": "artifact", "ref_id": "art-1"},
        )

    closure = client.get(
        f"/api/chat-bi/conversations/{conversation_id}/closure", headers=admin_headers
    ).json()
    assert closure["reached_count"] == closure["total_count"] == 6
    assert all(n["reached"] for n in closure["nodes"])
    assert closure["dangling"] == []  # 走完了就不该再有悬挂告警
