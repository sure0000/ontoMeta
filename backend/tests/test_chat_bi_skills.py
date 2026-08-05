"""Data Agent 技能层单测（V3 S1）。

覆盖：技能注册表、`select_skill` 的 overlay 叠加与工具解锁、`render_chart` 的接地校验
（x/y 必须是真实结果列、图型枚举、无数据即拒）。这些是「只解锁不收窄 + 图表不臆造列」
两条不变式的回归面。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.api.deps import chat_bi_service as svc
from app.services import chat_bi as c
from app.services.chat_bi import ChatBiService
from app.services.chat_bi_blocks import answer_to_blocks
from app.services.chat_bi_skills import SKILLS, skill_choices_text
from app.database import SessionLocal
from tests.fixtures.golden_questions import FinalTurn, ToolTurn
from tests.test_chat_bi_golden import _StubClient, _StubCompletions, _seed_golden_domain


def test_registry_has_overview_and_query():
    assert set(SKILLS) == {"overview", "query", "lineage", "create"}
    assert SKILLS["query"].extra_tool_names == ("render_chart",)
    assert SKILLS["overview"].extra_tool_names == ()
    assert SKILLS["lineage"].extra_tool_names == ("get_lineage",)
    assert SKILLS["create"].extra_tool_names == ("propose_draft", "lint_against_standard")
    assert "overview" in skill_choices_text() and "query" in skill_choices_text()


def test_create_skill_attaches_governance_and_lint_tool():
    """建数技能：解锁 lint_against_standard 自检工具 + 标了 attach_governance。"""
    create = SKILLS["create"]
    assert create.attach_governance is True
    tools = {t["function"]["name"] for t in c._tools_for_skill(create)}
    assert "lint_against_standard" in tools
    # 其它技能不带治理卡（scoped，不污染取数/概览）
    assert SKILLS["query"].attach_governance is False
    assert SKILLS["overview"].attach_governance is False


def test_select_create_skill_appends_governance_card():
    messages = [{"role": "system", "content": "BASE"}]
    skill, _result, _summary, is_error = svc._apply_select_skill(
        {"skill": "create"}, messages, "BASE", "【治理规约】命名 snake_case"
    )
    assert is_error is False and skill.name == "create"
    assert "【治理规约】命名 snake_case" in messages[0]["content"]


def test_select_non_governance_skill_ignores_card():
    """query 未标 attach_governance：即便传了卡也不并入（scoped）。"""
    messages = [{"role": "system", "content": "BASE"}]
    svc._apply_select_skill({"skill": "query"}, messages, "BASE", "【治理规约】不该出现")
    assert "【治理规约】不该出现" not in messages[0]["content"]


def test_tools_only_unlock_never_shrink():
    """只解锁不收窄：任何技能的工具集都 ⊇ 基础工具集。"""
    base = {t["function"]["name"] for t in c._BASE_TOOL_SCHEMAS}
    assert "select_skill" in base
    assert "render_chart" not in base  # 未选技能时图表工具不暴露
    overview_tools = {t["function"]["name"] for t in c._tools_for_skill(SKILLS["overview"])}
    query_tools = {t["function"]["name"] for t in c._tools_for_skill(SKILLS["query"])}
    assert base <= overview_tools
    assert base <= query_tools
    assert "render_chart" in query_tools
    assert "render_chart" not in overview_tools


def test_select_skill_appends_overlay_and_unlocks():
    messages = [{"role": "system", "content": "BASE"}]
    skill, result, summary, is_error = svc._apply_select_skill(
        {"skill": "query"}, messages, "BASE"
    )
    assert is_error is False
    assert skill is not None and skill.name == "query"
    assert messages[0]["content"].startswith("BASE\n\n")
    assert "取数分析技能" in messages[0]["content"]
    assert result["tools_unlocked"] == ["render_chart"]


def test_select_skill_reselect_replaces_not_stacks():
    """重复选取以最后一次为准，overlay 不叠加。"""
    messages = [{"role": "system", "content": "BASE"}]
    svc._apply_select_skill({"skill": "query"}, messages, "BASE")
    svc._apply_select_skill({"skill": "overview"}, messages, "BASE")
    assert messages[0]["content"].count("BASE") == 1
    assert "域概览技能" in messages[0]["content"]
    assert "取数分析技能" not in messages[0]["content"]


def test_select_skill_unknown_is_error_no_switch():
    messages = [{"role": "system", "content": "BASE"}]
    skill, result, summary, is_error = svc._apply_select_skill(
        {"skill": "nope"}, messages, "BASE"
    )
    assert is_error is True
    assert skill is None
    assert messages[0]["content"] == "BASE"
    assert "available" in result


_DR = {"columns": [{"key": "月份"}, {"key": "gmv"}], "rows": [{"月份": "1月", "gmv": 100}]}


def test_render_chart_valid_appends_spec():
    charts: list[dict] = []
    result, summary, is_error = svc._dispatch_render_chart(
        {"kind": "bar", "x": "月份", "y": "gmv", "title": "月度GMV"}, _DR, charts
    )
    assert is_error is False
    assert charts == [{"kind": "bar", "x": "月份", "y": "gmv", "title": "月度GMV"}]
    assert result["chart"]["kind"] == "bar"


def test_render_chart_rejects_unknown_kind():
    charts: list[dict] = []
    _result, _summary, is_error = svc._dispatch_render_chart(
        {"kind": "pie", "x": "月份", "y": "gmv"}, _DR, charts
    )
    assert is_error is True
    assert charts == []


def test_render_chart_rejects_invented_column():
    """接地：x/y 必须是真实结果列，臆造列被拒并回可用列。"""
    charts: list[dict] = []
    result, _summary, is_error = svc._dispatch_render_chart(
        {"kind": "line", "x": "季度", "y": "gmv"}, _DR, charts
    )
    assert is_error is True
    assert charts == []
    assert "available_columns" in result


def test_render_chart_rejects_when_no_data():
    charts: list[dict] = []
    _result, _summary, is_error = svc._dispatch_render_chart(
        {"kind": "bar", "x": "月份", "y": "gmv"}, None, charts
    )
    assert is_error is True
    assert charts == []


def test_skill_and_chart_flow_end_to_end(client):
    """端到端：select_skill(query) → run_sql → render_chart 穿过真实 agent 循环，
    payload 带 skill 与 charts，且投影出的 chart 块紧随结果表。"""
    domain_id, _onto, aliases = _seed_golden_domain()
    script = [
        ToolTurn([("select_skill", {"skill": "query"})]),
        ToolTurn([("run_sql", {"sql": "SELECT region, SUM(amount) AS gmv FROM t GROUP BY region"})]),
        ToolTurn([("render_chart", {"kind": "bar", "x": "region", "y": "gmv"})]),
        FinalTurn("汇总结果见上方图表。"),
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

    def fake_dispatch(db, *, domain_id, ontology_id, name, args, principal_role=None):
        # 只需喂 run_sql 一份可执行结果，让 render_chart 有真实列可作图。
        if name == "run_sql":
            return (
                {
                    "executed": True,
                    "sql": args.get("sql"),
                    "columns": [{"key": "region"}, {"key": "gmv"}],
                    "rows": [{"region": "华东", "gmv": 100}, {"region": "华北", "gmv": 80}],
                },
                "返回 2 行",
                False,
            )
        return ({"error": f"unexpected {name}"}, "", True)

    service._dispatch_agent_tool = fake_dispatch  # type: ignore[assignment]
    try:
        with SessionLocal() as db:
            payload = asyncio.run(
                service.ask(db, domain_id=domain_id, question="各区域销售额", principal_role="publisher")
            )
    finally:
        c.AsyncOpenAI = orig  # type: ignore[assignment]

    assert payload.get("grounding_refused") is not True, payload.get("answer")
    assert payload["skill"] == "query"
    assert payload["charts"] == [{"kind": "bar", "x": "region", "y": "gmv"}]
    assert payload["data_result"] and len(payload["data_result"]["rows"]) == 2

    types = [b["type"] for b in answer_to_blocks(payload)]
    assert "chart" in types and types.index("chart") > types.index("table")


def test_get_lineage_returns_neighborhood(client):
    """get_lineage 包 get_ontology_graph：中心对象 + 1 跳邻居 + 关系边。"""
    _domain, onto_id, aliases = _seed_golden_domain()
    order_id = aliases["@order"]
    with SessionLocal() as db:
        result, _summary, is_error = svc._dispatch_get_lineage(
            db, ontology_id=onto_id, args={"center_id": order_id, "depth": 1}
        )
    assert is_error is False
    node_ids = {n["id"] for n in result["nodes"]}
    assert order_id in node_ids
    assert aliases["@customer"] in node_ids  # 订单归属客户，1 跳邻居
    assert result["center_id"] == order_id
    assert result["edges"]


def test_get_lineage_rejects_unknown_center(client):
    _domain, onto_id, _aliases = _seed_golden_domain()
    with SessionLocal() as db:
        result, _summary, is_error = svc._dispatch_get_lineage(
            db, ontology_id=onto_id, args={"center_id": "nope"}
        )
    assert is_error is True
    assert "hint" in result


def test_lineage_skill_flow_end_to_end(client):
    """端到端：select_skill(lineage) → get_lineage 穿真实循环，payload 带 lineage，投影出 lineage 块。"""
    domain_id, onto_id, aliases = _seed_golden_domain()
    order_id = aliases["@order"]
    script = [
        ToolTurn([("select_skill", {"skill": "lineage"})]),
        ToolTurn([("get_lineage", {"center_id": order_id, "depth": 1})]),
        FinalTurn("该对象的上下游血缘见下方图。"),
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
                service.ask(db, domain_id=domain_id, question="订单的血缘", principal_role="publisher")
            )
    finally:
        c.AsyncOpenAI = orig  # type: ignore[assignment]

    assert payload.get("grounding_refused") is not True, payload.get("answer")
    assert payload["skill"] == "lineage"
    assert payload["lineage"] and payload["lineage"]["center_id"] == order_id
    assert "lineage" in [b["type"] for b in answer_to_blocks(payload)]


def test_propose_draft_builds_create_payload():
    r, summary, is_error = svc._dispatch_propose_draft(
        domain_id="dom1",
        args={"display_name": "复购率", "logic_type": "metric", "name": "repurchase_rate",
              "description": "90天内再次购买占比"},
    )
    assert is_error is False
    assert r["kind"] == "business_logic" and r["logic_type"] == "metric"
    cp = r["create_payload"]
    assert cp["domain_id"] == "dom1" and cp["name"] == "repurchase_rate"
    assert cp["display_name"] == "复购率" and cp["logic_type"] == "metric"


def test_propose_draft_rejects_bad_type_and_missing_name():
    _r, _s, e1 = svc._dispatch_propose_draft(domain_id="d", args={"display_name": "x", "logic_type": "nope"})
    assert e1 is True
    _r2, _s2, e2 = svc._dispatch_propose_draft(domain_id="d", args={"logic_type": "metric"})
    assert e2 is True


def test_propose_draft_derives_name_when_missing():
    r, _s, e = svc._dispatch_propose_draft(domain_id="d", args={"display_name": "复购率", "logic_type": "tag"})
    assert e is False
    assert r["create_payload"]["name"]  # 中文名派生占位标识符，非空


def test_dispatch_lint_flags_non_compliant_table():
    """自检工具：不合规约的物理表名回违规项 + 可照做 fix。"""
    with SessionLocal() as db:
        result, summary, is_error = svc._dispatch_lint(
            db, args={"kind": "transform", "spec": {"target_table": "DimCustomer"}}
        )
    assert is_error is False
    assert result["compliant"] is False
    codes = {v["code"] for v in result["violations"]}
    assert "naming_snake_case" in codes
    assert all(v.get("fix") for v in result["violations"])  # 每项带修法


def test_dispatch_lint_compliant_and_kopi_proposal_is_noop():
    with SessionLocal() as db:
        clean, _s, _e = svc._dispatch_lint(
            db, args={"spec": {"target_table": "dim_customer"}}
        )
        # 口径提案无 target_table → 无可查项，视为合规
        kopi, _s2, _e2 = svc._dispatch_lint(
            db, args={"spec": {"display_name": "复购率"}}
        )
    assert clean["compliant"] is True and clean["violations"] == []
    assert kopi["compliant"] is True


def test_dispatch_lint_rejects_non_object_spec():
    with SessionLocal() as db:
        result, _s, is_error = svc._dispatch_lint(db, args={"spec": "not-a-dict"})
    assert is_error is True
    assert "error" in result


def test_create_skill_flow_stays_read_only(client):
    """端到端：select_skill(create) → propose_draft 穿真实循环；payload 带提案，
    投影出 draft_proposal 块，且 **ask() 不新建任何 BusinessLogic**（只读边界）。"""
    from app.models import BusinessLogic

    domain_id, _onto, aliases = _seed_golden_domain()

    def _count_logics() -> int:
        with SessionLocal() as db:
            return db.query(BusinessLogic).count()

    before = _count_logics()
    script = [
        ToolTurn([("select_skill", {"skill": "create"})]),
        ToolTurn([("propose_draft", {"display_name": "复购率", "logic_type": "metric",
                                      "name": "repurchase_rate", "description": "90天内再次购买占比"})]),
        FinalTurn("已为你拟好「复购率」指标的建数提案，点确认即可创建为草稿。"),
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
                service.ask(db, domain_id=domain_id, question="建个复购率指标", principal_role="publisher")
            )
    finally:
        c.AsyncOpenAI = orig  # type: ignore[assignment]

    assert payload.get("grounding_refused") is not True, payload.get("answer")
    assert payload["skill"] == "create"
    assert payload["draft_proposals"] and payload["draft_proposals"][0]["display_name"] == "复购率"
    assert "draft_proposal" in [b["type"] for b in answer_to_blocks(payload)]
    # 只读不变式：propose_draft 只出提案，不落库
    assert _count_logics() == before
