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
    """一般性意图（general）下的无工具作答不被逆——那类问题本就豁免接地。"""
    domain_id, _o, aliases = _seed_golden_domain()
    script = [FinalTurn("我可以基于这个数据域的已发布本体回答业务问题、查数并解释口径。")]
    payload, completions, seen_user_msgs = _run(
        script, "你能做什么？", aliases, domain_id
    )
    assert not any("先用 search" in m for m in seen_user_msgs), "general 意图不该被逆"
    assert not payload.get("grounding_refused"), "general 意图不该被接地判定拒答"


# --------------------------------------------------------------------------- F4


def test_f4_confident_answer_without_tools_gets_verified_before_refusing():
    """V5 F4：长会话里模型照上一轮上下文自信作答、本轮一次工具没调。

    此前：文本不像拒答 → F2 逆不到 → 但 `grounded` 要求本轮有工具命中 → 那条**答对了的**
    答案被整段换成「未检索到匹配的对象类型」。用户看到拒答，模型其实答对了。
    现在：同样逆一次，要它拿本轮的凭证，于是答案能正常落地。
    """
    domain_id, _o, aliases = _seed_golden_domain()
    script = [
        # ① 不调工具、内容也不像拒答——正是老逻辑逆不到的那半
        FinalTurn("订单对象包含金额、状态、下单日期、客户ID 等字段。"),
        # ② 被逆后老老实实查一次
        ToolTurn([("get_object", {"object_id": "@order"})]),
        FinalTurn("订单对象包含金额、状态、下单日期、客户ID 等字段。"),
    ]
    payload, _c, seen_user_msgs = _run(
        script, "订单对象有哪些字段？", aliases, domain_id
    )
    assert any("还没调用任何工具" in m for m in seen_user_msgs), "F4 未逆核实"
    assert not payload.get("grounding_refused"), "核实后不该再被判未接地"
    assert any(
        s.get("tool") == "get_object" for s in (payload.get("steps") or [])
    ), "逆后应真的查了对象"


def test_f4_nudge_shares_the_one_shot_budget_with_f2():
    """逆只逆一次：F2 与 F4 共用同一个额度，逆完仍不查就照旧走接地判定拒答。"""
    domain_id, _o, aliases = _seed_golden_domain()
    script = [
        FinalTurn("订单对象包含金额、状态等字段。"),  # ① 无工具 → 逆
        FinalTurn("订单对象包含金额、状态等字段。"),  # ② 仍无工具 → 不再逆
    ]
    payload, _c, seen_user_msgs = _run(
        script, "订单对象有哪些字段？", aliases, domain_id
    )
    nudges = [m for m in seen_user_msgs if "先用 search" in m or "还没调用任何工具" in m]
    assert len(nudges) == 1, f"应只逆一次，实际 {len(nudges)}"
    assert payload.get("grounding_refused"), "始终未核实的答案仍应被接地判定拦下"
