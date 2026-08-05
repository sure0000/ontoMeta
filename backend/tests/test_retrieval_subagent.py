"""P4.2 检索子 agent：把「找的过程」关在隔离上下文里。

**要证的不是「答得更对」，是「主上下文更小」**。
`test_large_ontology_scale` 实测的基线是：一次典型检索序列往主上下文塞 ~8000 字符。
子 agent 做同样的事，主上下文只收到一份标识符清单（一两百字符）。

代价也要一并测出来：多花 LLM 调用。两个数分开计，才能判断什么时候该用它。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.database import SessionLocal
from app.services import agent_telemetry
from app.services.chat_bi import _AGENT_TOOL_SCHEMAS, ChatBiService
from app.services.retrieval_agent import (
    MAX_STEPS,
    RETRIEVAL_TOOLS,
    locate_entities,
)
from app.services.tool_result_compaction import compact_tool_result

from tests.fixtures.large_ontology import seed_large_ontology


# ---------------------------------------------------------------- stub


class _Fn:
    def __init__(self, name, args):
        self.name = name
        self.arguments = json.dumps(args, ensure_ascii=False)


class _Call:
    def __init__(self, i, name, args):
        self.id = f"c{i}"
        self.type = "function"
        self.function = _Fn(name, args)


class _Resp:
    def __init__(self, content="", tool_calls=None):
        self.choices = [
            SimpleNamespace(
                message=SimpleNamespace(content=content, tool_calls=tool_calls or None)
            )
        ]


class _StubClient:
    """按脚本回放子 agent 的行为。script 每项是 [(tool, args)…] 或最终 JSON 字符串。"""

    def __init__(self, script):
        self._script = list(script)
        self._i = 0
        self.calls = 0
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **kwargs):
        self.calls += 1
        if self._i >= len(self._script):
            return _Resp(content='{"objects": [], "logics": [], "reason": "耗尽"}')
        turn = self._script[self._i]
        self._i += 1
        if isinstance(turn, str):
            return _Resp(content=turn)
        return _Resp(tool_calls=[_Call(i, n, a) for i, (n, a) in enumerate(turn)])


@pytest.fixture(scope="module")
def env(client):
    """整个模块共用一个大本体：建一次就够，也避免域名唯一约束冲突。"""
    with SessionLocal() as db:
        return seed_large_ontology(db, name_suffix="-subagent")


def _run(env, script, intent="找出与销售订单相关的对象"):
    client = _StubClient(script)
    svc = ChatBiService()
    with SessionLocal() as db:
        res = asyncio.run(
            locate_entities(
                db, client=client, model="stub", intent=intent,
                domain_id=env.domain_id, ontology_id=env.ontology_id,
                dispatch=svc._dispatch_agent_tool,
                tool_schemas=_AGENT_TOOL_SCHEMAS,
                to_thread=_direct,
            )
        )
    return res, client


async def _direct(fn, *args, **kwargs):
    """测试里不需要真的丢线程池。"""
    return fn(*args, **kwargs)


# ---------------------------------------------------------------- 隔离效果


def test_only_conclusion_crosses_into_main_context(env):
    """**核心断言**：子 agent 烧掉的上下文远大于它交回主上下文的量。"""
    script = [
        [("search_objects", {"keyword": "销售"})],
        [("search_objects", {"keyword": "客户"})],
        [("search_relations", {"keyword": "归属"})],
        '{"objects": ["sales_order_000", "crm_customer_000"], "logics": ["sales_total"], '
        '"reason": "销售订单与客户是问题主体"}',
    ]
    res, _c = _run(env, script)

    assert res.objects == ["sales_order_000", "crm_customer_000"]
    assert res.logics == ["sales_total"]

    # 交回主上下文的量 = to_dict() 序列化后的字符数
    handed_back = len(compact_tool_result(res.to_dict(), 8000)[0])
    assert res.isolated_chars > 3000, "前提：检索过程确实烧掉了可观的上下文"
    assert handed_back < 600, f"交回主上下文 {handed_back} 字符，太多了"
    ratio = res.isolated_chars / handed_back
    assert ratio > 8, f"隔离比仅 {ratio:.1f}×，不值得多付 LLM 调用"
    print(
        f"\n[P4.2] 隔离 {res.isolated_chars} 字符 / 交回 {handed_back} 字符"
        f" = {ratio:.1f}× ；代价 {res.llm_calls} 次 LLM 调用"
    )


def test_cost_is_extra_llm_calls(env):
    """收益不是白来的：子 agent 每轮工具调用都要一次 LLM。这个代价必须可见。"""
    script = [
        [("search_objects", {"keyword": "销售"})],
        '{"objects": ["sales_order_000"], "logics": [], "reason": "x"}',
    ]
    res, stub = _run(env, script)
    assert res.llm_calls == 2 == stub.calls
    assert res.steps == 1


# ---------------------------------------------------------------- 职责边界


def test_subagent_cannot_use_non_retrieval_tools(env):
    """越权工具必须被明确回绝——静默忽略会让模型一直重试。"""
    script = [
        [("run_sql", {"sql": "SELECT 1"})],
        '{"objects": [], "logics": [], "reason": "无法取数"}',
    ]
    res, _c = _run(env, script)
    assert res.steps == 0, "越权调用不计入检索步数"
    assert "run_sql" not in RETRIEVAL_TOOLS


def test_only_retrieval_schemas_are_exposed(client):
    """子 agent 拿到的工具表必须是主 agent 的**子集**，且只含检索类。"""
    names = {t["function"]["name"] for t in _AGENT_TOOL_SCHEMAS}
    assert set(RETRIEVAL_TOOLS) <= names
    assert "compile_metric" not in RETRIEVAL_TOOLS
    assert "profile_values" not in RETRIEVAL_TOOLS
    assert "ask_clarification" not in RETRIEVAL_TOOLS


# ---------------------------------------------------------------- 结论解析


def test_conclusion_wrapped_in_prose_is_extracted(env):
    """模型常在 JSON 外裹一层话；能抽出来就抽，抽不出宁可空结论也不把正文当结论。"""
    script = [
        '好的，我找到了：\n```json\n{"objects": ["sales_order_000"], "logics": [], '
        '"reason": "相关"}\n```\n希望有帮助',
    ]
    res, _c = _run(env, script)
    assert res.objects == ["sales_order_000"]


def test_unparseable_conclusion_yields_empty_not_garbage(env):
    res, _c = _run(env, ["我觉得可能跟订单有关系吧"])
    assert res.objects == [] and res.logics == []
    assert "未得出" in res.reason or "解析失败" in res.reason


def test_budget_exhaustion_still_asks_for_conclusion(env):
    """步数耗尽也要拿到结论，而不是空手而归。"""
    script = [[("search_objects", {"keyword": "销售"})]] * MAX_STEPS + [
        '{"objects": ["sales_order_000"], "logics": [], "reason": "耗尽后收敛"}'
    ]
    res, _c = _run(env, script)
    assert res.objects == ["sales_order_000"]
    assert res.llm_calls == MAX_STEPS + 1


# ---------------------------------------------------------------- 接进主 agent


def test_main_agent_records_subagent_cost_separately(env):
    """遥测要把子 agent 的开销**单独计**，否则只看到 LLM 调用涨了、看不到省了什么。"""
    agent_telemetry.reset()
    stub = _StubClient([
        [("search_objects", {"keyword": "销售"})],
        '{"objects": ["sales_order_000"], "logics": [], "reason": "x"}',
    ])
    svc = ChatBiService()
    tel = agent_telemetry.RunTelemetry()
    with SessionLocal() as db:
        result, summary, is_error = asyncio.run(
            svc._dispatch_locate_entities(
                db, client=stub, model="stub",
                domain_id=env.domain_id, ontology_id=env.ontology_id,
                args={"intent": "销售订单"}, telemetry=tel,
            )
        )
    assert not is_error, result
    assert result["objects"] == ["sales_order_000"]
    assert "未进入本对话上下文" in result["note"]
    assert tel.subagent_runs == 1
    assert tel.subagent_llm_calls == 2
    assert tel.subagent_isolated_chars > 0
    # 主循环的 llm_calls 不该被子 agent 污染
    assert tel.llm_calls == 0


def test_subagent_failure_degrades_with_a_usable_hint(env):
    """子 agent 挂了要给出可执行的下一步，而不是让主 agent 卡住。"""

    class _Boom:
        def __init__(self):
            self.chat = SimpleNamespace(completions=self)

        async def create(self, **kwargs):
            raise RuntimeError("upstream down")

    svc = ChatBiService()
    tel = agent_telemetry.RunTelemetry()
    with SessionLocal() as db:
        result, _summary, is_error = asyncio.run(
            svc._dispatch_locate_entities(
                db, client=_Boom(), model="stub",
                domain_id=env.domain_id, ontology_id=env.ontology_id,
                args={"intent": "销售订单"}, telemetry=tel,
            )
        )
    assert is_error
    assert "search_objects" in result["fix"]
