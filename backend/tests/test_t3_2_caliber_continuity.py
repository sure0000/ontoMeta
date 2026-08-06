"""V5 T3.2：合成长会话 golden——摘要后「延续上一轮口径继续下钻」不重算、不误拒。

固化 O1 + T3 的联合行为进 CI：早前轮定过一条口径 SQL，会话变长后被 compaction 摘要，
但 T3 把那条 SQL 完整保留进摘要 → 模型在延续轮**看得到**它、能直接复用，
而不是因为「看不到上一轮口径」而重新推导或误拒答。

不依赖真实 LLM：stub 固定模型行为；断言压在「摘要保住了 SQL、且其标识符入了账本
（延续轮引用旧口径不被 F4 误判幻觉）」这条确定性链路上。
"""

from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace

from app.database import SessionLocal
from app.services.chat_bi import ChatBiService
import app.services.chat_bi as chat_bi_mod
from app.services.agent_compaction import compact_conversation
from app.services.agent_grounding import FactLedger

from tests.test_chat_bi_golden import _StubClient, _StubCompletions, _seed_golden_domain
from tests.fixtures.golden_questions import FinalTurn, ToolTurn


# 早前轮定下的口径 SQL（跨 order/customer 的 GMV），要能穿过 compaction 活到延续轮。
_GMV_SQL = (
    "SELECT c.customer_name, SUM(o.amount) AS gmv "
    "FROM order o JOIN customer c ON o.customer_id = c.id "
    "WHERE o.status = 'paid' GROUP BY c.customer_name ORDER BY gmv DESC"
)


def _long_history_with_caliber() -> list[dict]:
    """构造一段长会话：第 1 轮定 GMV 口径 SQL，随后几轮无关长问答把它挤进被摘要区。

    填充量要超真实的 agent_history_char_budget（默认 6000），才能在真实 ask 里触发 compaction。
    """
    hist = [
        {"role": "user", "content": "帮我算每个客户的 GMV（按客户名分组的已付款金额合计）"},
        {"role": "assistant", "content": f"已用如下口径：\n```sql\n{_GMV_SQL}\n```\n按客户名聚合已付款订单金额。"},
    ]
    # 每轮 ~1200 字符×6 轮 ≈ 7200+，连同首轮总量稳超 6000 预算
    for i in range(6):
        hist.append({"role": "user", "content": f"顺便问个无关的问题{i}" + "补" * 600})
        hist.append({"role": "assistant", "content": f"这是无关回答{i}" + "答" * 600})
    return hist


def test_t3_2_compaction_preserves_caliber_sql_for_followup():
    """核心：会话变长后 compaction 触发，早前那条 GMV 口径 SQL 被完整保留进摘要。"""
    hist = _long_history_with_caliber()
    comp = compact_conversation(hist, char_budget=1500)

    assert comp.triggered, "长会话应触发 compaction"
    assert comp.summarized_turns >= 2
    # T3：口径 SQL 完整保留（含 GROUP BY / ORDER BY 尾巴，非首句截断）
    assert len(comp.key_sql) == 1
    assert "GROUP BY c.customer_name" in comp.key_sql[0]
    assert "ORDER BY gmv DESC" in comp.key_sql[0]
    assert _GMV_SQL in (comp.summary or ""), "口径 SQL 应完整进摘要，供延续轮复用"


def test_t3_2_preserved_sql_identifiers_grounded():
    """摘要保留 SQL 的表/列标识符入 FactLedger——延续轮复述旧口径不被 F4 误判幻觉。"""
    hist = _long_history_with_caliber()
    comp = compact_conversation(hist, char_budget=1500)
    assert comp.key_sql

    # 复刻 chat_bi 的入账逻辑
    ledger = FactLedger()
    idents: list[str] = []
    for sql in comp.key_sql:
        for tok in re.findall(r"[A-Za-z_][A-Za-z0-9_.]*", sql):
            idents.append(tok)
            if "." in tok:
                idents.append(tok.rsplit(".", 1)[-1])
    ledger.add_context_name(*idents)

    # 延续轮若引用旧口径涉及的表/列，应已接地
    for name in ("order", "customer", "amount", "customer_name", "gmv"):
        assert ledger.has_entity_named(name), f"{name} 应因保留 SQL 而接地"


def test_t3_2_followup_reuses_caliber_not_refuse():
    """端到端：带长会话 history 进 ask，延续轮基于保留口径下钻（按月拆），不 0 步误拒。"""
    domain_id, _o, aliases = _seed_golden_domain()
    hist = _long_history_with_caliber()

    # 延续轮脚本：模型直接复用口径改写（加 GROUP BY 月）→ 作答，不重新 search/refuse
    monthly_sql = _GMV_SQL.replace(
        "GROUP BY c.customer_name",
        "GROUP BY c.customer_name, strftime('%Y-%m', o.order_date)",
    )
    script = [
        FinalTurn(
            "沿用上一轮 GMV 口径，按月拆开如下：\n```sql\n" + monthly_sql + "\n```"
        ),
    ]
    completions = _StubCompletions(script, aliases)

    seen_system: list[str] = []
    orig = completions.create

    async def _spy(**kwargs):
        if not kwargs.get("stream"):
            for m in kwargs.get("messages", []):
                if m.get("role") == "system":
                    seen_system.append(str(m.get("content") or ""))
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
                svc.ask(
                    db,
                    domain_id=domain_id,
                    question="把刚才的 GMV 口径按月拆开",
                    history=hist,
                    principal_role="publisher",
                )
            )
    finally:
        chat_bi_mod.AsyncOpenAI = orig_cls  # type: ignore[assignment]

    # 摘要（含保留的口径 SQL）确实进了发给模型的 system 上下文
    joined = "\n".join(seen_system)
    assert "已确定的口径 SQL" in joined, "compaction 摘要的口径 SQL 段应进模型上下文"
    assert _GMV_SQL in joined, "延续轮应看得到上一轮完整口径 SQL"
    # 不是 0 步误拒：拿到了答案（复用口径的按月 SQL）
    assert payload.get("answer"), "延续轮应正常作答而非空/拒"
    assert "gmv" in (payload.get("suggested_sql") or payload.get("answer") or "").lower()
