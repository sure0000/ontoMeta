"""V4 S1 集成测：驱动 agent 循环，验证 run_sql 大结果**离场**——回给模型的消息里
是句柄+样例而非全量行，且 read_result 能分页取回全量。复用 golden 的 stub/种子机制。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from app.database import SessionLocal
from app.services.chat_bi import ChatBiService

from tests.test_chat_bi_golden import (
    _StubClient,
    _StubCompletions,
    _seed_golden_domain,
)
from tests.fixtures.golden_questions import FinalTurn, ToolTurn


def _big_executed_result(n: int = 100) -> dict:
    return {
        "executed": True,
        "sql": "SELECT amount FROM order",
        "columns": [{"key": "amount", "title": "金额"}],
        "rows": [{"amount": i} for i in range(n)],
        "row_count": n,
        "truncated": True,
        "proved": {"tables": ["order"], "columns": ["order.amount"]},
    }


def test_run_sql_result_offloaded_and_read_result_pages():
    import app.services.chat_bi as chat_bi_mod

    domain_id, _onto_id, aliases = _seed_golden_domain()

    # 脚本：run_sql → read_result(取尾部行) → 收尾作答
    script = [
        ToolTurn([("run_sql", {"sql": "SELECT amount FROM order"})]),
        ToolTurn([("read_result", {"handle": "rs_1", "offset": 95, "limit": 5})]),
        FinalTurn("共 100 行，尾部样例已核对。"),
    ]
    completions = _StubCompletions(script, aliases)

    # 捕获每次发给模型的 messages 与 tools 快照
    seen_messages: list[list[dict]] = []
    seen_tools: list[list[str]] = []
    orig_create = completions.create

    async def _spy_create(**kwargs):
        if not kwargs.get("stream"):
            seen_messages.append([dict(m) for m in kwargs.get("messages", [])])
            seen_tools.append(
                [t["function"]["name"] for t in (kwargs.get("tools") or [])]
            )
        return await orig_create(**kwargs)

    completions.create = _spy_create  # type: ignore[assignment]

    original_client_cls = chat_bi_mod.AsyncOpenAI
    chat_bi_mod.AsyncOpenAI = lambda **_kw: _StubClient(completions)  # type: ignore[assignment]

    svc = ChatBiService()
    svc.settings_service = SimpleNamespace(  # type: ignore[assignment]
        get_llm_runtime=lambda _db: SimpleNamespace(
            api_key="k", api_base_url="http://stub", model="stub"
        )
    )
    # 让 run_sql 直接返回一份「已执行的大结果」——不依赖真实数据源。
    svc._dispatch_run_sql = lambda *a, **k: (  # type: ignore[assignment]
        _big_executed_result(100), "返回 100 行", False
    )

    try:
        with SessionLocal() as db:
            payload = asyncio.run(
                svc.ask(db, domain_id=domain_id, question="计算订单金额明细", principal_role="publisher")
            )
    finally:
        chat_bi_mod.AsyncOpenAI = original_client_cls  # type: ignore[assignment]

    # 找到 run_sql 的 tool 结果消息（离场后应含句柄、无第 99 行）
    tool_msgs = [
        m for msgs in seen_messages for m in msgs if m.get("role") == "tool"
    ]
    run_sql_msg = next((m for m in tool_msgs if "result_handle" in str(m.get("content"))), None)
    assert run_sql_msg is not None, "run_sql 结果未离场（消息里找不到 result_handle）"
    content = str(run_sql_msg["content"])
    assert '"amount": 99' not in content, "全量行不应进入上下文"
    assert '"amount": 0' in content, "样例首行应在上下文"
    assert "read_result" in content

    # read_result 的结果消息应含尾部行（第 95..99 行）
    read_msg = next((m for m in tool_msgs if '"offset": 95' in str(m.get("content"))), None)
    assert read_msg is not None, "read_result 未回分页结果"
    read_content = json.loads(read_msg["content"])
    assert read_content["rows"][0] == {"amount": 95}
    assert read_content["returned"] == 5
    assert read_content["has_more"] is False

    # 前端仍拿全量 data_result（渲染不掉真数据）
    assert payload["data_result"]["rows"][-1] == {"amount": 99}
    assert len(payload["data_result"]["rows"]) == 100

    # V4 O3 渐进披露：read_result 不在首轮工具集，run_sql 执行后才出现。
    assert "read_result" not in seen_tools[0], "read_result 不该在首轮暴露"
    assert any("read_result" in tools for tools in seen_tools[1:]), "run_sql 后应解锁 read_result"
