"""V4 S2 单测：通用子 agent 骨架（O4）+ 取数探路子 agent（scout）。

复用 golden 里没有的一套轻量 stub：脚本每项是 [(tool,args)…] 或最终文本。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.database import SessionLocal
from app.services import agent_telemetry
from app.services.agent_subagent import SubAgentSpec, run_subagent
from app.services.chat_bi import _AGENT_TOOL_SCHEMAS, ChatBiService
from app.services.query_scout_agent import (
    MAX_STEPS,
    SCOUT_TOOLS,
    ScoutResult,
    scout_query,
)

from tests.fixtures.large_ontology import seed_large_ontology


# ------------------------------------------------------------------ stub


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
    def __init__(self, script):
        self._script = list(script)
        self._i = 0
        self.calls = 0
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **kwargs):
        self.calls += 1
        if self._i >= len(self._script):
            return _Resp(content='{"sql": "", "brief": "耗尽", "objects": [], "logics": []}')
        turn = self._script[self._i]
        self._i += 1
        if isinstance(turn, str):
            return _Resp(content=turn)
        return _Resp(tool_calls=[_Call(i, n, a) for i, (n, a) in enumerate(turn)])


async def _direct(fn, *args, **kwargs):
    return fn(*args, **kwargs)


@pytest.fixture(scope="module")
def env(client):
    with SessionLocal() as db:
        return seed_large_ontology(db, name_suffix="-scout")


# ------------------------------------------------------------------ 通用骨架


def test_generic_runner_rejects_disallowed_tools_without_counting_step(env):
    """越权工具：明确回绝、不计步数（否则模型会一直重试）。"""
    spec = SubAgentSpec(
        name="测试", system_prompt="sys", allowed_tools=("search_objects",), max_steps=3
    )
    client = _StubClient([
        [("run_sql", {"sql": "SELECT 1"})],  # 越权
        "done",
    ])
    svc = ChatBiService()
    with SessionLocal() as db:
        run = asyncio.run(run_subagent(
            db, client=client, model="stub", spec=spec, user_prompt="go",
            domain_id=env.domain_id, ontology_id=env.ontology_id,
            dispatch=svc._subagent_dispatch, tool_schemas=_AGENT_TOOL_SCHEMAS,
            to_thread=_direct,
        ))
    assert run.steps == 0
    assert run.final_text == "done"
    assert run.isolated_chars > 0  # 回绝消息也在隔离上下文里流动过


def test_generic_runner_budget_exhaustion_forces_conclusion(env):
    spec = SubAgentSpec(
        name="测试", system_prompt="sys", allowed_tools=("search_objects",), max_steps=2
    )
    client = _StubClient([
        [("search_objects", {"keyword": "销售"})],
        [("search_objects", {"keyword": "客户"})],
        "final after exhaustion",
    ])
    svc = ChatBiService()
    with SessionLocal() as db:
        run = asyncio.run(run_subagent(
            db, client=client, model="stub", spec=spec, user_prompt="go",
            domain_id=env.domain_id, ontology_id=env.ontology_id,
            dispatch=svc._subagent_dispatch, tool_schemas=_AGENT_TOOL_SCHEMAS,
            to_thread=_direct,
        ))
    assert run.llm_calls == 3  # 2 轮工具 + 1 轮强制收尾
    assert run.steps == 2
    assert run.final_text == "final after exhaustion"


# ------------------------------------------------------------------ scout


def _run_scout(env, script, intent="统计各客户的销售总额"):
    client = _StubClient(script)
    svc = ChatBiService()
    with SessionLocal() as db:
        res = asyncio.run(scout_query(
            db, client=client, model="stub", intent=intent,
            domain_id=env.domain_id, ontology_id=env.ontology_id,
            dispatch=svc._subagent_dispatch, tool_schemas=_AGENT_TOOL_SCHEMAS,
            to_thread=_direct,
        ))
    return res, client


def test_scout_only_candidate_sql_crosses_into_main(env):
    """探路的 profile/find_join 试错在隔离上下文；只回候选 SQL + 要点。"""
    script = [
        [("search_objects", {"keyword": "销售"})],
        [("find_join_path", {"from_keyword": "销售", "to_keyword": "客户"})],
        '{"sql": "SELECT c.name, SUM(o.amount) FROM sales_order_000 o JOIN crm_customer_000 c '
        'ON o.customer=c.id GROUP BY c.name", "brief": "按客户聚合销售额", '
        '"objects": ["sales_order_000", "crm_customer_000"], "logics": []}',
    ]
    res, _c = _run_scout(env, script)
    assert res.sql.startswith("SELECT")
    assert res.objects == ["sales_order_000", "crm_customer_000"]
    d = res.to_dict()
    assert d["candidate_sql"].startswith("SELECT")
    assert "run_sql" in d["note"] and "未进入本对话上下文" in d["note"]
    assert res.isolated_chars > 0


def test_scout_cannot_execute_sql(env):
    """scout 无 run_sql——越权调用被回绝、不计步。"""
    assert "run_sql" not in SCOUT_TOOLS
    script = [
        [("run_sql", {"sql": "SELECT 1"})],
        '{"sql": "", "brief": "不该执行", "objects": [], "logics": []}',
    ]
    res, _c = _run_scout(env, script)
    assert res.steps == 0


def test_scout_unparseable_yields_empty_candidate(env):
    res, _c = _run_scout(env, ["大概查一下订单吧"])
    assert res.sql == ""
    assert "未得出" in res.brief or "解析失败" in res.brief


def test_scout_dispatch_records_subagent_cost_separately(env):
    agent_telemetry.reset()
    stub = _StubClient([
        [("profile_values", {"object_keyword": "销售", "property_keyword": "状态"})],
        '{"sql": "SELECT 1", "brief": "x", "objects": ["sales_order_000"], "logics": []}',
    ])
    svc = ChatBiService()
    tel = agent_telemetry.RunTelemetry()
    with SessionLocal() as db:
        result, _summary, is_error = asyncio.run(svc._dispatch_scout_query(
            db, client=stub, model="stub",
            domain_id=env.domain_id, ontology_id=env.ontology_id,
            args={"intent": "统计销售"}, telemetry=tel,
        ))
    assert not is_error, result
    assert result["candidate_sql"] == "SELECT 1"
    assert tel.subagent_runs == 1
    assert tel.subagent_llm_calls == 2
    assert tel.subagent_isolated_chars > 0
    assert tel.llm_calls == 0  # 主循环不被子 agent 污染


def test_scout_failure_degrades_with_hint(env):
    class _Boom:
        def __init__(self):
            self.chat = SimpleNamespace(completions=self)

        async def create(self, **kwargs):
            raise RuntimeError("down")

    svc = ChatBiService()
    tel = agent_telemetry.RunTelemetry()
    with SessionLocal() as db:
        result, _summary, is_error = asyncio.run(svc._dispatch_scout_query(
            db, client=_Boom(), model="stub",
            domain_id=env.domain_id, ontology_id=env.ontology_id,
            args={"intent": "x"}, telemetry=tel,
        ))
    assert is_error
    assert "profile_values" in result["fix"]
# T5 追加到 tests/test_query_scout_subagent.py 末尾


def test_scout_can_iterate_profile_then_rewrite_within_budget(env):
    """V5 T5：探路→取样→按样例改写→再取，整条链在隔离上下文里跑完。

    老预算 5 轮：定位 1 + join 1 + profile 1 + 改写后复查 1 + 收尾 1 —— 正好卡死，
    稍一试错就被强制收尾，交回的是没验完的草稿。新预算给到 8 轮，链能跑完。
    """
    script = [
        [("search_objects", {"keyword": "销售"})],
        [("find_join_path", {"from_keyword": "销售", "to_keyword": "客户"})],
        [("profile_values", {"object_keyword": "销售", "property_keyword": "状态"})],
        [("get_object", {"object_id": "sales_order_000"})],
        [("profile_values", {"object_keyword": "销售", "property_keyword": "金额"})],
        [("compile_metric", {"keyword": "销售额"})],
        '{"sql": "SELECT 1", "brief": "改写后已复查", "objects": ["sales_order_000"], "logics": []}',
    ]
    res, client = _run_scout(env, script)
    assert res.sql == "SELECT 1"
    assert res.steps == 6, f"6 步链应跑完，实际 {res.steps}"
    assert client.calls == 7  # 6 轮工具 + 1 轮结论，未触发强制收尾


def test_scout_budget_still_caps_the_chain(env):
    """预算放大不等于无上限：超出 MAX_STEPS 仍被强制收尾（护住 LLM 调用不失控）。"""
    script = [[("search_objects", {"keyword": f"k{i}"})] for i in range(MAX_STEPS + 3)]
    script.append('{"sql": "", "brief": "被截断", "objects": [], "logics": []}')
    res, client = _run_scout(env, script)
    assert res.steps == MAX_STEPS, f"应封顶在 {MAX_STEPS} 步，实际 {res.steps}"
    assert client.calls == MAX_STEPS + 1  # 预算内每轮 1 次 + 1 次强制收尾


def test_scout_return_shape_unchanged_by_t5(env):
    """T5.2：返回结构不变——主 agent 的执行链路不该因为 scout 变多步而要改。"""
    res, _c = _run_scout(env, [
        '{"sql": "SELECT 1", "brief": "b", "objects": ["o"], "logics": ["l"]}'
    ])
    assert set(res.to_dict()) == {"candidate_sql", "brief", "objects", "logics", "note"}
