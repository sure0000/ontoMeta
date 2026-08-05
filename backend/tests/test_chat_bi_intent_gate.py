"""Data Agent 意图门控单测（结构性问题不被追加取数 SQL）。

覆盖架构强制的三层：
1. `_classify_intent` 规则分类（analytical 赢平局、fail-open）。
2. `_tools_for_skill(..., sql_allowed=False)` 收窄——从工具集移除 run_sql/compile_metric。
3. 端到端穿真实 agent 循环：结构性问题下 dispatch 硬拒取数工具、正文示例 SQL 不被提升；
   取数问题不受影响；显式 select_skill('query') 经升级阀重新放开取数。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.database import SessionLocal
from app.services import chat_bi as c
from app.services.chat_bi import ChatBiService
from app.services.chat_bi_blocks import answer_to_blocks
from tests.fixtures.golden_questions import FinalTurn, ToolTurn
from tests.test_chat_bi_golden import _StubClient, _StubCompletions, _seed_golden_domain


# --------------------------------------------------------------------------- 单元


def test_classify_intent_structural():
    """纯元数据/结构问题 → structural。"""
    cls = ChatBiService._classify_intent
    assert cls("订单有哪些属性") == "structural"
    assert cls("客户对象包含哪些字段") == "structural"
    assert cls("订单和客户是什么关系") == "structural"
    assert cls("订单总额这个口径的定义") == "structural"


def test_classify_intent_analytical():
    """取数问题 → analytical。"""
    cls = ChatBiService._classify_intent
    assert cls("近30天订单总额是多少") == "analytical"
    assert cls("各区域销售额对比") == "analytical"
    assert cls("统计每个客户的订单数量") == "analytical"


def test_classify_intent_analytical_wins_ties():
    """混合措辞里 analytical 赢平局（fail-open，绝不误伤真实取数）。"""
    cls = ChatBiService._classify_intent
    # 「字段」是结构标记、「多少」是取数标记 → 取数
    assert cls("订单有多少个字段") == "analytical"
    assert cls("各业务对象的属性数量统计") == "analytical"


def test_classify_intent_defaults_analytical():
    """都不命中 → analytical（fail-open）。"""
    assert ChatBiService._classify_intent("帮我看看这个") == "analytical"
    assert ChatBiService._classify_intent("") == "analytical"


def test_tools_narrow_removes_sql_tools_when_not_allowed():
    """sql_allowed=False：取数工具（run_sql/compile_metric）被移出工具集。"""
    full = {t["function"]["name"] for t in c._tools_for_skill(None, sql_allowed=True)}
    assert {"run_sql", "compile_metric"} <= full  # 默认放开时都在

    narrowed = {t["function"]["name"] for t in c._tools_for_skill(None, sql_allowed=False)}
    assert "run_sql" not in narrowed
    assert "compile_metric" not in narrowed
    # 其余检索/元数据工具不受影响
    assert {"get_object", "search_objects", "select_skill"} <= narrowed


def test_tools_narrow_applies_under_skill_too():
    """带技能时收窄仍生效：query 技能解锁的 render_chart 保留，取数工具仍被剥离。"""
    from app.services.chat_bi_skills import SKILLS

    narrowed = {
        t["function"]["name"]
        for t in c._tools_for_skill(SKILLS["query"], sql_allowed=False)
    }
    assert "render_chart" in narrowed
    assert "run_sql" not in narrowed and "compile_metric" not in narrowed


def test_tools_default_unchanged_for_existing_callers():
    """默认 sql_allowed=True：既有「只解锁不收窄」不变式不动。"""
    base = {t["function"]["name"] for t in c._BASE_TOOL_SCHEMAS}
    default = {t["function"]["name"] for t in c._tools_for_skill(None)}
    assert default == base


# --------------------------------------------------------------------------- 端到端


def _make_service(completions: _StubCompletions) -> ChatBiService:
    """装配一个跑 stub LLM 的服务；verify 放行以隔离「取数门控」这一维度。"""
    service = ChatBiService()
    service._completions = completions  # type: ignore[attr-defined]  # 供 _ask 取用
    service.settings_service = SimpleNamespace(  # type: ignore[assignment]
        get_llm_runtime=lambda _db: SimpleNamespace(
            api_key="stub-key", api_base_url="http://stub", model="stub-model"
        )
    )
    service._resolve_domain_data_source = lambda _db: None  # type: ignore[assignment]
    # 接地校验与本测正交：放行，避免拒答改写盖掉我们要观察的 suggested_sql/data_result。
    service._verify_answer = lambda answer, ledger, question: (True, [])  # type: ignore[assignment]
    return service


def _ask(service: ChatBiService, domain_id: str, question: str) -> dict:
    orig = c.AsyncOpenAI
    c.AsyncOpenAI = lambda **_k: _StubClient(service._completions)  # type: ignore[attr-defined,assignment]
    try:
        with SessionLocal() as db:
            return asyncio.run(
                service.ask(db, domain_id=domain_id, question=question, principal_role="publisher")
            )
    finally:
        c.AsyncOpenAI = orig  # type: ignore[assignment]


def _obj_detail(order_id: str) -> dict:
    """get_object 的最小返回——带 id 即可让 referenced_objects 接地。"""
    return {"id": order_id, "name": "order", "display_name": "订单",
            "properties": [{"name": "amount", "display_name": "金额"}]}


def test_structural_question_hard_rejects_run_sql(client):
    """结构性问题：模型即便发 run_sql 也在 dispatch 层被硬拒，不落到取数分发、无数据结果、无 SQL 块。"""
    domain_id, _onto, aliases = _seed_golden_domain()
    order_id = aliases["@order"]
    called = {"run_sql": False}

    def fake_dispatch(db, *, domain_id, ontology_id, name, args, principal_role=None):
        if name == "get_object":
            return (_obj_detail(order_id), "对象「订单」1 字段", False)
        if name == "run_sql":
            called["run_sql"] = True  # 不该发生——门控应在此之前拦下
            return ({"executed": True, "columns": [], "rows": []}, "", False)
        return ({"error": f"unexpected {name}"}, "", True)

    script = [
        ToolTurn([("get_object", {"object_id": "@order"})]),
        ToolTurn([("run_sql", {"sql": "SELECT * FROM orders"})]),
        FinalTurn("订单包含金额、状态、下单日期、客户ID等属性。"),
    ]
    service = _make_service(_StubCompletions(script, aliases))
    service._dispatch_agent_tool = fake_dispatch  # type: ignore[assignment]

    payload = _ask(service, domain_id, "订单有哪些属性")

    assert called["run_sql"] is False, "结构性问题下 run_sql 不应被分发执行"
    assert not payload.get("data_result")
    assert payload.get("suggested_sql") is None
    types = [b["type"] for b in answer_to_blocks(payload)]
    assert "sql" not in types and "table" not in types
    # 门控留痕：run_sql 步以失败记录
    assert any(
        s.get("tool") == "run_sql" and s.get("status") == "failed"
        for s in payload.get("steps") or []
    )


def test_structural_prose_sql_fence_not_promoted(client):
    """结构性问题：模型在正文写的示例 ```sql 不得被提升成取数块。"""
    domain_id, _onto, aliases = _seed_golden_domain()
    order_id = aliases["@order"]

    def fake_dispatch(db, *, domain_id, ontology_id, name, args, principal_role=None):
        if name == "get_object":
            return (_obj_detail(order_id), "对象「订单」1 字段", False)
        return ({"error": f"unexpected {name}"}, "", True)

    script = [
        ToolTurn([("get_object", {"object_id": "@order"})]),
        FinalTurn("订单包含金额、状态等属性。\n\n```sql\nSELECT amount FROM orders\n```"),
    ]
    service = _make_service(_StubCompletions(script, aliases))
    service._dispatch_agent_tool = fake_dispatch  # type: ignore[assignment]

    payload = _ask(service, domain_id, "订单有哪些属性")

    assert payload.get("suggested_sql") is None
    assert "sql" not in [b["type"] for b in answer_to_blocks(payload)]


def test_analytical_prose_sql_fence_still_promoted(client):
    """取数问题：门控不误伤——正文示例 SQL 仍被收割为 suggested_sql 并渲染 SQL 块。"""
    domain_id, _onto, aliases = _seed_golden_domain()
    order_id = aliases["@order"]

    def fake_dispatch(db, *, domain_id, ontology_id, name, args, principal_role=None):
        if name == "get_object":
            return (_obj_detail(order_id), "对象「订单」1 字段", False)
        return ({"error": f"unexpected {name}"}, "", True)

    script = [
        ToolTurn([("get_object", {"object_id": "@order"})]),
        FinalTurn("订单总额约为 100。\n\n```sql\nSELECT SUM(amount) FROM orders\n```"),
    ]
    service = _make_service(_StubCompletions(script, aliases))
    service._dispatch_agent_tool = fake_dispatch  # type: ignore[assignment]

    payload = _ask(service, domain_id, "订单总额是多少")

    assert payload.get("suggested_sql") and "SELECT" in payload["suggested_sql"].upper()
    assert "sql" in [b["type"] for b in answer_to_blocks(payload)]


def test_escalation_via_query_skill_reenables_sql(client):
    """升级阀：结构性问题下模型显式 select_skill('query') 后，run_sql 重新可执行。"""
    domain_id, _onto, aliases = _seed_golden_domain()

    def fake_dispatch(db, *, domain_id, ontology_id, name, args, principal_role=None):
        if name == "run_sql":
            return (
                {"executed": True, "sql": args.get("sql"),
                 "columns": [{"key": "gmv"}], "rows": [{"gmv": 100}]},
                "返回 1 行", False,
            )
        return ({"error": f"unexpected {name}"}, "", True)

    script = [
        ToolTurn([("select_skill", {"skill": "query"})]),
        ToolTurn([("run_sql", {"sql": "SELECT SUM(amount) AS gmv FROM orders"})]),
        FinalTurn("按你的要求已取数，订单总额为 100。"),
    ]
    service = _make_service(_StubCompletions(script, aliases))
    service._dispatch_agent_tool = fake_dispatch  # type: ignore[assignment]

    payload = _ask(service, domain_id, "订单有哪些属性")  # 起始判为结构性

    assert payload.get("skill") == "query"
    assert payload.get("data_result") and payload["data_result"]["rows"] == [{"gmv": 100}]
