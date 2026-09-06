"""结果表态经 MCP 回写：**执行成功不等于结果正确**。

系统这一侧的事实（status / 回执 / Airflow 终态）答不了「搬过来的数对不对」——回执自陈
成功而数据没搬对，本仓真出过（见 receipt-failure-vs-artifact-status）。这份判断只能来自人。

按会话组织的决策账本退场后，它落在制品自己身上。通用 agent 的职责是把判断依据摆给用户、
问出答复、原样回写——**不是替他判断**。这里钉的就是这条边界。
"""

from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import mcp.types as types
import pytest

from app.database import SessionLocal
from app.mcp import server as mcp_server
from app.mcp.tools import AuthContext
from app.models.agent import ArtifactStatus, GovernanceArtifact


def _body(result):
    return json.loads(result.content[0].text)


@pytest.fixture(autouse=True)
def reset_mcp_auth():
    yield
    mcp_server._reset_session_auth()


@pytest.fixture
def call_via_server(monkeypatch):
    def _call(name: str, arguments: dict, role: str | None = "editor"):
        mcp_server._reset_session_auth()
        monkeypatch.setattr(
            mcp_server,
            "resolve_auth_context",
            lambda: AuthContext(
                client_type="mcp_local", role=role, principal_name=f"mcp-{role}"
            ),
        )
        params = types.CallToolRequestParams(name=name, arguments=arguments)
        return asyncio.run(mcp_server.handle_call_tool(None, params))

    return _call


@pytest.fixture
def finished_task():
    """一条已经跑成功、但还没有人说对不对的任务。"""
    with SessionLocal() as db:
        artifact = GovernanceArtifact(
            kind="sync",
            name=f"结果表态-{uuid4().hex[:6]}",
            status=ArtifactStatus.SUCCEEDED.value,
            spec_json=json.dumps({"target_ods_table": "ods_erp_order"}),
            execution_receipt_json=json.dumps({"ok": True, "rows": 1200}),
        )
        db.add(artifact)
        db.commit()
        task_id = artifact.id
    yield task_id
    with SessionLocal() as db:
        row = db.get(GovernanceArtifact, task_id)
        if row is not None:
            db.delete(row)
            db.commit()


def test_status_alone_leaves_the_verdict_open(call_via_server, finished_task):
    """跑成功的任务默认**没有**结果判断，并明确点名"还没人看过"。

    不点出来，agent 就会拿 succeeded 当"用户满意"把整件事结掉。
    """
    body = _body(call_via_server("get_task_status", {"task_id": finished_task}))
    assert body["success"] is True
    assert body["data"]["status"] == "succeeded"
    assert body["data"]["result"]["outcome"] is None

    pending = body["data"]["result_pending"]
    # 判断依据要和问题一起摆出来，人才判得了。
    assert pending["evidence"]["status"] == "succeeded"
    assert pending["evidence"]["receipt_summary"]
    assert "不要替用户判断" in pending["instruction"]


def test_agent_writes_back_the_user_answer(call_via_server, finished_task):
    """agent 问过用户之后回写；署名与来路都记下来。"""
    body = _body(
        call_via_server(
            "confirm_task_result",
            {
                "task_id": finished_task,
                "outcome": "rejected",
                "note": "只搬了本月，上个月的没进来",
            },
        )
    )
    assert body["success"] is True
    assert body["data"]["result"]["outcome"] == "rejected"
    assert body["data"]["result"]["note"] == "只搬了本月，上个月的没进来"
    # 经 agent 转述与人自己点，可信度不同，读的人有权知道。
    assert body["data"]["result"]["via"] == "mcp_local"
    assert body["data"]["result"]["confirmed_by"] == "mcp-editor"
    # 表态不改任务状态：任务确实跑成功了，只是结果不对。
    assert body["data"]["status"] == "succeeded"

    after = _body(call_via_server("get_task_status", {"task_id": finished_task}))
    assert after["data"]["result"]["outcome"] == "rejected"
    assert "result_pending" not in after["data"]


def test_rejected_without_a_reason_is_refused(call_via_server, finished_task):
    """说"不符合"却不说哪里不符合，这条记录对后来看的人没有用。"""
    body = _body(
        call_via_server(
            "confirm_task_result", {"task_id": finished_task, "outcome": "rejected"}
        )
    )
    assert body["success"] is False
    assert "note" in body["error"]


def test_verdict_needs_a_terminal_task(call_via_server):
    """还在跑的任务谈不上"结果对不对"。"""
    with SessionLocal() as db:
        artifact = GovernanceArtifact(
            kind="sync",
            name=f"未终态-{uuid4().hex[:6]}",
            status=ArtifactStatus.EXECUTING.value,
        )
        db.add(artifact)
        db.commit()
        task_id = artifact.id
    try:
        body = _body(
            call_via_server(
                "confirm_task_result", {"task_id": task_id, "outcome": "accepted"}
            )
        )
        assert body["success"] is False
        assert "还没有结果" in body["error"]
    finally:
        with SessionLocal() as db:
            row = db.get(GovernanceArtifact, task_id)
            if row is not None:
                db.delete(row)
                db.commit()


def test_reader_cannot_write_a_verdict(call_via_server, finished_task):
    """记的是人的判断，仍然是一次写——reader 不该能写。"""
    result = call_via_server(
        "confirm_task_result",
        {"task_id": finished_task, "outcome": "accepted"},
        role="reader",
    )
    assert result.is_error is True
    assert "editor" in _body(result)["error"]
