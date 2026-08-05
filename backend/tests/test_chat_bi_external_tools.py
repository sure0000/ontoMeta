"""P4：配置驱动的外部工具（免改代码扩展 Data Agent 能力）单测。

覆盖：注册校验（命名/原生冲突/重复）、schema 投影与域作用域、HTTP executor（mock httpx，
含结果封顶与错误降级）、启停/删除、以及端到端——模型调用外部工具后答案被判**接地**（不拒答）。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.api.deps import chat_bi_service as svc
from app.services import chat_bi as c
from app.services import chat_bi_external_tools as ext
from app.services.chat_bi import ChatBiService
from app.database import SessionLocal
from tests.fixtures.golden_questions import FinalTurn, ToolTurn
from tests.test_chat_bi_golden import _StubClient, _StubCompletions, _seed_golden_domain


@pytest.fixture(autouse=True)
def _clean_external_tools():
    """共享 DB 无逐测重置：清空外部工具表，避免全局工具/唯一名跨测泄漏。"""
    from app.models import ChatBiExternalTool

    with SessionLocal() as db:
        db.query(ChatBiExternalTool).delete()
        db.commit()
    yield


class _FakeResp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _fake_httpx(resp: _FakeResp):
    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None, headers=None):
            return resp

        def get(self, url, params=None, headers=None):
            return resp

    return SimpleNamespace(Client=_Client)


def test_register_rejects_bad_name_native_collision_and_dup(client):
    domain_id, _onto, _aliases = _seed_golden_domain()
    with SessionLocal() as db:
        with pytest.raises(ext.ExternalToolError):
            ext.register_tool(db, name="BadName", description="d", url="https://x")  # 非 snake_case
        with pytest.raises(ext.ExternalToolError):
            ext.register_tool(db, name="run_sql", description="d", url="https://x")  # 原生冲突
        with pytest.raises(ext.ExternalToolError):
            ext.register_tool(db, name="dq_check", description="d", url="ftp://x")  # 非 http(s)
        ext.register_tool(db, name="dq_check", description="d", url="https://x")
        with pytest.raises(ext.ExternalToolError):
            ext.register_tool(db, name="dq_check", description="d2", url="https://y")  # 重复


def test_schema_gen_and_domain_scope(client):
    domain_id, _onto, _aliases = _seed_golden_domain()
    with SessionLocal() as db:
        ext.register_tool(db, name="global_tool", description="全局", url="https://g")
        ext.register_tool(
            db, name="domain_tool", description="仅本域", url="https://d",
            domain_id=domain_id, parameters={"type": "object", "properties": {"q": {"type": "string"}}},
        )
        names_here = ext.external_tool_names_for_domain(db, domain_id)
        names_other = ext.external_tool_names_for_domain(db, "other-domain")
        schemas = ext.tool_schemas_for_domain(db, domain_id)
    assert names_here == {"global_tool", "domain_tool"}
    assert names_other == {"global_tool"}  # 域工具不泄漏到别域
    by_name = {s["function"]["name"]: s for s in schemas}
    assert by_name["domain_tool"]["function"]["parameters"]["properties"]["q"]["type"] == "string"


def test_call_external_tool_success_and_gates(client, monkeypatch):
    domain_id, _onto, _aliases = _seed_golden_domain()
    with SessionLocal() as db:
        ext.register_tool(db, name="dq_check", description="d", url="https://x", domain_id=domain_id)

        monkeypatch.setattr(ext, "httpx", _fake_httpx(_FakeResp(200, {"score": 95})))
        res, _s, err = ext.call_external_tool(db, tool_name="dq_check", domain_id=domain_id, args={"t": "orders"})
        assert err is False and res["data"] == {"score": 95}

        # 未知/未启用 → error（不抛）
        _r2, _s2, err2 = ext.call_external_tool(db, tool_name="nope", domain_id=domain_id, args={})
        assert err2 is True
        # 别域不可见
        _r3, _s3, err3 = ext.call_external_tool(db, tool_name="dq_check", domain_id="other", args={})
        assert err3 is True


def test_call_external_tool_http_error_and_cap(client, monkeypatch):
    domain_id, _onto, _aliases = _seed_golden_domain()
    with SessionLocal() as db:
        ext.register_tool(db, name="big_tool", description="d", url="https://x",
                          domain_id=domain_id, result_max_chars=200)
        # 巨大 JSON → 封顶为截断文本
        monkeypatch.setattr(ext, "httpx", _fake_httpx(_FakeResp(200, {"blob": "x" * 5000})))
        res, _s, err = ext.call_external_tool(db, tool_name="big_tool", domain_id=domain_id, args={})
        assert err is False and len(str(res["data"])) <= 200
        # 5xx → is_error
        monkeypatch.setattr(ext, "httpx", _fake_httpx(_FakeResp(500, {"m": "boom"})))
        _r2, _s2, err2 = ext.call_external_tool(db, tool_name="big_tool", domain_id=domain_id, args={})
        assert err2 is True


def test_toggle_and_delete(client):
    domain_id, _onto, _aliases = _seed_golden_domain()
    with SessionLocal() as db:
        row = ext.register_tool(db, name="dq_check", description="d", url="https://x", domain_id=domain_id)
        tid = row.id
        ext.set_enabled(db, tid, False)
        assert ext.external_tool_names_for_domain(db, domain_id) == set()  # 停用不注入
        ext.set_enabled(db, tid, True)
        assert "dq_check" in ext.external_tool_names_for_domain(db, domain_id)
        assert ext.delete_tool(db, tid) is True
        assert ext.get_tool(db, tid) is None


def test_external_tool_flow_grounds_answer(client, monkeypatch):
    """端到端：模型调用外部工具 → 结果登记进账本 + grounded_hit=True，答案不被误判未接地拒答。"""
    domain_id, _onto, aliases = _seed_golden_domain()
    with SessionLocal() as db:
        ext.register_tool(db, name="dq_check", description="数据质量检查", url="https://x", domain_id=domain_id)
    monkeypatch.setattr(ext, "httpx", _fake_httpx(_FakeResp(200, {"score": 95, "table": "orders"})))

    script = [
        ToolTurn([("dq_check", {"table": "orders"})]),
        FinalTurn("已通过外部工具完成数据质量检查。"),
    ]
    completions = _StubCompletions(script, aliases)
    orig = c.AsyncOpenAI
    c.AsyncOpenAI = lambda **_k: _StubClient(completions)  # type: ignore[assignment]
    service = ChatBiService()
    service.settings_service = SimpleNamespace(  # type: ignore[assignment]
        get_llm_runtime=lambda _db: SimpleNamespace(
            api_key="stub-key", api_base_url="http://stub", model="stub-model"
        )
    )
    service._resolve_domain_data_source = lambda _db: None  # type: ignore[assignment]
    try:
        with SessionLocal() as db:
            payload = asyncio.run(
                service.ask(db, domain_id=domain_id, question="orders 表数据质量如何", principal_role="publisher")
            )
    finally:
        c.AsyncOpenAI = orig  # type: ignore[assignment]

    assert payload.get("grounding_refused") is not True, payload.get("answer")
    assert "外部工具" in payload["answer"]
    # 外部工具调用出现在步骤轨迹里
    assert any(s.get("tool") == "dq_check" for s in payload.get("steps") or [])


def test_external_tools_absent_when_none_registered(client):
    """无外部工具时工具集不变（零回归）。"""
    domain_id, _onto, _aliases = _seed_golden_domain()
    with SessionLocal() as db:
        assert ext.tool_schemas_for_domain(db, domain_id) == []
        assert ext.external_tool_names_for_domain(db, domain_id) == set()


def test_register_endpoint_masks_secret(client, admin_headers):
    domain_id, _onto, _aliases = _seed_golden_domain()
    resp = client.post(
        "/api/chat-bi/external-tools",
        json={"name": "ticket_create", "description": "建工单", "url": "https://x",
              "auth_header": "Bearer super-secret", "domain_id": domain_id},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["has_auth"] is True
    assert "auth_header" not in body  # 机密不回显
    assert "super-secret" not in resp.text
