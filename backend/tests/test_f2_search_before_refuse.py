"""V5 F2 集成测：结构性问题「未搜就拒」时，先逆一次「先 search 再判」再作答。

驱真实 agent 循环（stub LLM）：第 1 轮无工具、内容像拒答 → 应被 F2 逆一次，
模型第 2 轮 search_objects → 命中后正常作答，而非 0 步拒答。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.database import SessionLocal
from app.services.chat_bi import ChatBiService
import app.services.chat_bi as chat_bi_mod

from tests.test_chat_bi_golden import _StubClient, _StubCompletions, _seed_golden_domain
from tests.fixtures.golden_questions import FinalTurn, ToolTurn


def _run(script, question, aliases, domain_id):
    completions = _StubCompletions(script, aliases)
    seen_user_msgs: list[str] = []
    orig = completions.create

    async def _spy(**kwargs):
        if not kwargs.get("stream"):
            for m in kwargs.get("messages", []):
                if m.get("role") == "user":
                    seen_user_msgs.append(str(m.get("content") or ""))
        return await orig(**kwargs)

    completions.create = _spy  # type: ignore[assignment]
    orig_cls = chat_bi_mod.AsyncOpenAI
    chat_bi_mod.AsyncOpenAI = lambda **_k: _StubClient(completions)  # type: ignore[assignment]
    svc = ChatBiService()
    svc.settings_service = SimpleNamespace(  # type: ignore[assignment]
        get_llm_runtime=lambda _db: SimpleNamespace(
            api_key="k", api_base_url="http://s", model="stub"
        )
    )
    svc._resolve_domain_data_source = lambda _db: None  # type: ignore[assignment]
    try:
        with SessionLocal() as db:
            payload = asyncio.run(
                svc.ask(db, domain_id=domain_id, question=question, principal_role="editor")
            )
    finally:
        chat_bi_mod.AsyncOpenAI = orig_cls  # type: ignore[assignment]
    return payload, completions, seen_user_msgs


def test_f2_structural_refusal_gets_search_nudge():
    """结构性问题：第 1 轮直接「未找到」→ F2 逆一次 → 第 2 轮 search → 正常作答。"""
    domain_id, _o, aliases = _seed_golden_domain()
    # 脚本：①无工具、像拒答 → ②被逆后 search_objects → ③拿到结果后作答
    script = [
        FinalTurn("很抱歉，未找到名为「订单」的对象。"),
        ToolTurn([("search_objects", {"keyword": "订单"})]),
        FinalTurn("订单对象包含金额、状态等字段。"),
    ]
    payload, completions, seen_user_msgs = _run(
        script, "订单对象有哪些字段？", aliases, domain_id
    )
    # F2 逆了一次：出现了「先别下结论…先用 search」的追加 user 消息
    assert any("先用 search" in m for m in seen_user_msgs), "F2 未逆搜索"
    # 最终没被 0 步拒答：走到了 search + 作答
    steps = payload.get("steps") or []
    assert any(s.get("tool") == "search_objects" for s in steps), "逆后应真的 search 了"


def test_f2_nudge_fires_at_most_once():
    """F2 只逆一次：若逆后仍不搜、继续拒答，不再无限逆。"""
    domain_id, _o, aliases = _seed_golden_domain()
    script = [
        FinalTurn("很抱歉，未找到相关对象。"),   # 第 1 轮拒答 → 逆
        FinalTurn("确实未找到，无法回答。"),       # 逆后仍拒答 → 放行，不再逆
    ]
    payload, completions, seen_user_msgs = _run(
        script, "客户对象有哪些字段？", aliases, domain_id
    )
    nudges = [m for m in seen_user_msgs if "先用 search" in m]
    assert len(nudges) == 1, f"F2 应只逆一次，实际 {len(nudges)}"


def test_f2_normal_answer_not_nudged():
    """正常给出实体内容的答案（非拒答）不被 F2 逆。"""
    domain_id, _o, aliases = _seed_golden_domain()
    script = [FinalTurn("市场活动对象用于管理营销活动，包含名称、起止时间等字段。")]
    payload, completions, seen_user_msgs = _run(
        script, "市场活动对象有哪些字段？", aliases, domain_id
    )
    assert not any("先用 search" in m for m in seen_user_msgs), "正常答案不该被逆"
