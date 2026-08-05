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
    assert set(SKILLS) == {"overview", "query", "lineage", "create", "task"}
    assert SKILLS["query"].extra_tool_names == ("update_plan", "analyze_result", "render_chart")
    assert SKILLS["overview"].extra_tool_names == ()
    assert SKILLS["lineage"].extra_tool_names == ("get_lineage",)
    assert SKILLS["create"].extra_tool_names == ("propose_draft", "lint_against_standard")
    assert SKILLS["task"].extra_tool_names == (
        "propose_action", "get_task_status", "lint_against_standard"
    )
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
    assert result["tools_unlocked"] == ["update_plan", "analyze_result", "render_chart"]


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


# ---------------- P0：数据任务提案 + 追踪（task 技能） ----------------


def test_propose_action_builds_draft_payload():
    r, _summary, is_error = svc._dispatch_propose_action(
        ontology_id="onto1", domain_id="dom1",
        args={"kind": "materialize", "intent": "把客户主数据物化到 dim_customer",
              "context": {"target_table": "dim_customer"}},
    )
    assert is_error is False
    assert r["kind"] == "materialize"
    dp = r["draft_payload"]
    assert dp["kind"] == "materialize" and dp["ontology_id"] == "onto1"
    assert dp["intent"] == "把客户主数据物化到 dim_customer"
    assert dp["context"] == {"target_table": "dim_customer"}


def test_propose_action_rejects_bad_kind_and_missing_intent():
    # cluster 不在数据任务白名单（归基建，不由聊天 agent 提）
    _r, _s, e1 = svc._dispatch_propose_action(
        ontology_id="o", domain_id="d", args={"kind": "cluster", "intent": "x"}
    )
    assert e1 is True
    _r2, _s2, e2 = svc._dispatch_propose_action(
        ontology_id="o", domain_id="d", args={"kind": "materialize"}
    )
    assert e2 is True  # 缺 intent


def test_task_skill_unlocks_action_tools_and_governance():
    task = SKILLS["task"]
    assert task.attach_governance is True
    tools = {t["function"]["name"] for t in c._tools_for_skill(task)}
    base = {t["function"]["name"] for t in c._BASE_TOOL_SCHEMAS}
    assert base <= tools  # 只解锁不收窄
    assert {"propose_action", "get_task_status", "lint_against_standard"} <= tools


def test_get_task_status_reads_and_filters_by_ontology(client):
    from app.models.agent import ArtifactStatus, GovernanceArtifact

    with SessionLocal() as db:
        a = GovernanceArtifact(
            kind="transform", name="加工客户表", ontology_id="onto-A",
            intent="i", spec_json="{}", status=ArtifactStatus.SUCCEEDED.value,
            execution_receipt_json='{"rows": 42, "dag_run_id": "r1"}',
        )
        b = GovernanceArtifact(
            kind="transform", name="别域任务", ontology_id="onto-B",
            intent="i", spec_json="{}", status=ArtifactStatus.FAILED.value,
        )
        db.add_all([a, b])
        db.commit()
        a_id, b_id = a.id, b.id

        # 列表：仅本体 A 的任务，回执摘要含关键字段
        res, _s, err = svc._dispatch_get_task_status(db, ontology_id="onto-A", args={})
        assert err is False
        assert a_id in {t["id"] for t in res["tasks"]}
        assert "别域任务" not in {t["name"] for t in res["tasks"]}

        # 单个：状态 + 回执摘要
        one, _s2, err2 = svc._dispatch_get_task_status(
            db, ontology_id="onto-A", args={"artifact_id": a_id}
        )
        assert err2 is False
        t = one["tasks"][0]
        assert t["status"] == "succeeded" and "42" in (t["receipt_summary"] or "")

        # 跨本体访问被拒（不泄漏别域任务）
        _r3, _s3, err3 = svc._dispatch_get_task_status(
            db, ontology_id="onto-A", args={"artifact_id": b_id}
        )
        assert err3 is True


def test_task_skill_flow_stays_read_only(client):
    """端到端：select_skill(task) → propose_action 穿真实循环；payload 带任务提案，
    投影出 action_proposal 块，且 **ask() 不新建任何 GovernanceArtifact**（只读边界）。"""
    from app.models.agent import GovernanceArtifact

    domain_id, _onto, aliases = _seed_golden_domain()

    def _count() -> int:
        with SessionLocal() as db:
            return db.query(GovernanceArtifact).count()

    before = _count()
    script = [
        ToolTurn([("select_skill", {"skill": "task"})]),
        ToolTurn([("propose_action", {"kind": "materialize",
                                       "intent": "把客户主数据物化到 dim_customer",
                                       "context": {"target_table": "dim_customer"}})]),
        FinalTurn("已拟好物化任务提案，确认后即可创建并运行。"),
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
                service.ask(db, domain_id=domain_id, question="把客户主数据物化落库",
                            principal_role="publisher")
            )
    finally:
        c.AsyncOpenAI = orig  # type: ignore[assignment]

    assert payload.get("grounding_refused") is not True, payload.get("answer")
    assert payload["skill"] == "task"
    assert payload["action_proposals"]
    assert payload["action_proposals"][0]["kind"] == "materialize"
    assert payload["action_proposals"][0]["draft_payload"]["ontology_id"]
    assert "action_proposal" in [b["type"] for b in answer_to_blocks(payload)]
    # 只读不变式：propose_action 只出提案，不建制品
    assert _count() == before


# ---------------- P1：跨轮任务记忆（会话 ↔ 任务关联） ----------------


def test_link_conversation_task_is_idempotent(client):
    from app.models.agent import ArtifactStatus, GovernanceArtifact

    domain_id, onto_id, _aliases = _seed_golden_domain()
    with SessionLocal() as db:
        conv = svc.create_conversation(db, domain_id=domain_id, title="t")
        cid = conv["id"]
        a = GovernanceArtifact(
            kind="materialize", name="物化客户", ontology_id=onto_id,
            intent="i", spec_json="{}", status=ArtifactStatus.DRAFTED.value,
        )
        db.add(a)
        db.commit()
        a_id = a.id

        svc.link_conversation_task(db, cid, a_id, kind="materialize", intent="i")
        svc.link_conversation_task(db, cid, a_id, kind="materialize", intent="i")
        ids = svc.list_conversation_task_ids(db, cid)
        assert ids == [a_id]  # 幂等：不重复


def test_get_task_status_prefers_conversation_scope(client):
    """P1：给了 conversation_id 且未指定 artifact_id → 优先本会话催生的任务，
    而非整个本体的最近任务。"""
    from app.models.agent import ArtifactStatus, GovernanceArtifact

    domain_id, onto_id, _aliases = _seed_golden_domain()
    with SessionLocal() as db:
        conv = svc.create_conversation(db, domain_id=domain_id, title="t")
        cid = conv["id"]
        mine = GovernanceArtifact(
            kind="materialize", name="本会话的物化", ontology_id=onto_id,
            intent="i", spec_json="{}", status=ArtifactStatus.SUCCEEDED.value,
            execution_receipt_json='{"rows": 7}',
        )
        other = GovernanceArtifact(
            kind="transform", name="别处的加工", ontology_id=onto_id,
            intent="i", spec_json="{}", status=ArtifactStatus.FAILED.value,
        )
        db.add_all([mine, other])
        db.commit()
        mine_id = mine.id
        svc.link_conversation_task(db, cid, mine_id, kind="materialize")

        # 会话作用域：只回本会话的任务
        res, _s, err = svc._dispatch_get_task_status(
            db, ontology_id=onto_id, args={}, conversation_id=cid
        )
        assert err is False
        assert res["scope"] == "conversation"
        assert {t["id"] for t in res["tasks"]} == {mine_id}

        # 无会话上下文：回落本体最近任务（含两者）
        res2, _s2, err2 = svc._dispatch_get_task_status(db, ontology_id=onto_id, args={})
        assert err2 is False
        assert res2["scope"] == "ontology"
        assert {t["id"] for t in res2["tasks"]} >= {mine_id}


def test_link_task_endpoint(client, admin_headers):
    from app.models.agent import ArtifactStatus, GovernanceArtifact

    domain_id, onto_id, _aliases = _seed_golden_domain()
    with SessionLocal() as db:
        conv = svc.create_conversation(db, domain_id=domain_id, title="t")
        cid = conv["id"]
        a = GovernanceArtifact(
            kind="sync", name="同步任务", ontology_id=onto_id,
            intent="i", spec_json="{}", status=ArtifactStatus.DRAFTED.value,
        )
        db.add(a)
        db.commit()
        a_id = a.id

    resp = client.post(
        f"/api/chat-bi/conversations/{cid}/tasks",
        json={"artifact_id": a_id, "kind": "sync", "intent": "同步"},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["linked"] is True

    # 会话不存在 → 404
    missing = client.post(
        "/api/chat-bi/conversations/nope/tasks",
        json={"artifact_id": a_id},
        headers=admin_headers,
    )
    assert missing.status_code == 404


# ---------------- P2：显式规划（update_plan / plan 块） ----------------


def test_query_skill_unlocks_update_plan():
    tools = {t["function"]["name"] for t in c._tools_for_skill(SKILLS["query"])}
    assert "update_plan" in tools and "render_chart" in tools
    # 计划工具不在基础集（未选技能时不暴露），且不在非分析技能
    base = {t["function"]["name"] for t in c._BASE_TOOL_SCHEMAS}
    assert "update_plan" not in base
    assert "update_plan" not in {t["function"]["name"] for t in c._tools_for_skill(SKILLS["overview"])}


def test_update_plan_normalizes_and_caps():
    r, _s, e = svc._dispatch_update_plan(
        {"steps": [
            {"title": "看整体量级"},
            {"title": "按月拆趋势", "status": "active"},
            {"title": "定位异常月", "status": "bogus"},  # 非法状态归 pending
            "环比验证",  # 裸字符串也接受
        ], "note": "自顶向下"}
    )
    assert e is False
    statuses = [s["status"] for s in r["plan"]["steps"]]
    assert statuses == ["pending", "active", "pending", "pending"]
    assert r["plan"]["note"] == "自顶向下"


def test_update_plan_rejects_empty():
    _r, _s, e1 = svc._dispatch_update_plan({"steps": []})
    assert e1 is True
    _r2, _s2, e2 = svc._dispatch_update_plan({"steps": [{"title": "  "}]})  # 无有效标题
    assert e2 is True


def test_plan_flow_end_to_end(client):
    """端到端：select_skill(query) → update_plan → run_sql 穿真实循环；payload 带 plan，
    投影出 plan 块（紧随 steps 轨迹）。计划本身不接地——靠 run_sql 命中本体避免拒答。"""
    domain_id, _onto, aliases = _seed_golden_domain()
    script = [
        ToolTurn([("select_skill", {"skill": "query"})]),
        ToolTurn([("update_plan", {"steps": [
            {"title": "取各区域销售额", "status": "active"},
            {"title": "对比找异常"},
        ], "note": "先总后分"})]),
        ToolTurn([("run_sql", {"sql": "SELECT region, SUM(amount) AS gmv FROM t GROUP BY region"})]),
        FinalTurn("华东明显高于其它区域，详见上表。"),
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

    real_dispatch = service._dispatch_agent_tool

    def fake_dispatch(db, *, domain_id, ontology_id, name, args, principal_role=None):
        if name == "run_sql":
            return (
                {"executed": True, "sql": args.get("sql"),
                 "columns": [{"key": "region"}, {"key": "gmv"}],
                 "rows": [{"region": "华东", "gmv": 100}, {"region": "华北", "gmv": 80}]},
                "返回 2 行", False,
            )
        # update_plan 无需 db/上下文，走真实 dispatch
        return real_dispatch(
            db, domain_id=domain_id, ontology_id=ontology_id, name=name,
            args=args, principal_role=principal_role,
        )

    service._dispatch_agent_tool = fake_dispatch  # type: ignore[assignment]
    try:
        with SessionLocal() as db:
            payload = asyncio.run(
                service.ask(db, domain_id=domain_id, question="探索各区域销售额有没有异常",
                            principal_role="publisher")
            )
    finally:
        c.AsyncOpenAI = orig  # type: ignore[assignment]

    assert payload.get("grounding_refused") is not True, payload.get("answer")
    assert payload["skill"] == "query"
    assert payload["plan"] and len(payload["plan"]["steps"]) == 2
    assert payload["plan"]["steps"][0]["status"] == "active"

    types = [b["type"] for b in answer_to_blocks(payload)]
    assert "plan" in types
    if "steps" in types:  # 计划块紧随执行轨迹
        assert types.index("plan") == types.index("steps") + 1


# ---------------- P3：跨会话记忆（按域高频，注入软提示） ----------------


def test_record_domain_memory_accumulates_and_gates(client):
    from app.models.chat_bi import ChatBiDomainMemory

    domain_id, _onto, _aliases = _seed_golden_domain()
    hit = {
        "referenced_objects": [{"id": "o1", "display_name": "客户"}],
        "referenced_logics": [{"id": "l1", "display_name": "成交额"}],
    }
    with SessionLocal() as db:
        svc.record_domain_memory(db, domain_id, hit)
        svc.record_domain_memory(db, domain_id, hit)  # 再命中一次 → 累加
        # 拒答不记
        svc.record_domain_memory(
            db, domain_id,
            {"grounding_refused": True, "referenced_objects": [{"id": "o2", "display_name": "X"}]},
        )
        # mock 不记
        svc.record_domain_memory(
            db, domain_id,
            {"used_mock": True, "referenced_objects": [{"id": "o3", "display_name": "Y"}]},
        )
        rows = db.query(ChatBiDomainMemory).filter_by(domain_id=domain_id).all()
        by = {(r.ref_kind, r.ref_id): r.hit_count for r in rows}
    assert by[("object_type", "o1")] == 2
    assert by[("business_logic", "l1")] == 2
    assert ("object_type", "o2") not in by  # 拒答未记
    assert ("object_type", "o3") not in by  # mock 未记


def test_build_domain_memory_card_top_n_and_empty(client):
    domain_id, _onto, _aliases = _seed_golden_domain()
    with SessionLocal() as db:
        for _ in range(3):
            svc.record_domain_memory(db, domain_id, {"referenced_objects": [{"id": "hot", "display_name": "订单"}]})
        svc.record_domain_memory(db, domain_id, {"referenced_objects": [{"id": "cold", "display_name": "冷门对象"}]})
        svc.record_domain_memory(db, domain_id, {"referenced_logics": [{"id": "m", "display_name": "成交额"}]})
        card = svc.build_domain_memory_card(db, domain_id, limit=8)
        empty = svc.build_domain_memory_card(db, "no-such-domain")
    assert "本域高频" in card
    assert "常用对象" in card and "订单" in card
    assert "常用口径" in card and "成交额" in card
    assert empty == ""  # 无记忆返回空串（不污染提示）


def test_domain_memory_injected_into_system_prompt(client):
    """端到端：先沉淀记忆，再问一次；系统提示 messages[0] 应含本域高频软提示。"""
    domain_id, _onto, aliases = _seed_golden_domain()
    with SessionLocal() as db:
        for _ in range(2):
            svc.record_domain_memory(db, domain_id, {"referenced_objects": [{"id": "o1", "display_name": "客户档案"}]})

    script = [FinalTurn("已了解。")]
    completions = _StubCompletions(script, aliases)
    seen: dict = {}
    orig_create = completions.create

    async def capturing_create(**kwargs):
        if "system" not in seen and kwargs.get("messages"):
            seen["system"] = kwargs["messages"][0]["content"]
        return await orig_create(**kwargs)

    completions.create = capturing_create  # type: ignore[assignment]
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
            asyncio.run(service.ask(db, domain_id=domain_id, question="随便问问", principal_role="publisher"))
    finally:
        c.AsyncOpenAI = orig  # type: ignore[assignment]

    assert "本域高频" in seen.get("system", "")
    assert "客户档案" in seen["system"]


# ---------------- P5：结果统计分析 / 离群检测（analyze_result / insight 块） ----------------


def test_query_skill_unlocks_analyze_result():
    tools = {t["function"]["name"] for t in c._tools_for_skill(SKILLS["query"])}
    assert "analyze_result" in tools
    base = {t["function"]["name"] for t in c._BASE_TOOL_SCHEMAS}
    assert "analyze_result" not in base  # 未选技能不暴露


def test_analyze_result_stats_and_outliers():
    dr = {"columns": [{"key": "gmv"}, {"key": "region"}],
          "rows": [{"gmv": v, "region": "x"} for v in [10, 11, 9, 10, 12, 10, 11, 200]]}
    analyses: list = []
    r, _s, e = svc._dispatch_analyze_result({}, dr, analyses)
    assert e is False
    cols = {c0["column"]: c0 for c0 in r["analysis"]["columns"]}
    assert "gmv" in cols and "region" not in cols  # 只分析数值列
    gmv = cols["gmv"]
    assert gmv["count"] == 8
    assert gmv["min"] == 9.0 and gmv["max"] == 200.0
    assert 200.0 in gmv["outliers"] and gmv["outlier_count"] >= 1
    assert r["analysis"]["total_outliers"] >= 1
    assert analyses  # 追加到累加器


def test_analyze_result_rejects_no_data_and_no_numeric():
    _r, _s, e1 = svc._dispatch_analyze_result({}, {"rows": []}, [])
    assert e1 is True
    _r2, _s2, e2 = svc._dispatch_analyze_result(
        {}, {"columns": [{"key": "name"}], "rows": [{"name": "a"}, {"name": "b"}]}, []
    )
    assert e2 is True  # 无数值列


def test_analyze_flow_end_to_end(client):
    """端到端：select_skill(query) → run_sql → analyze_result 穿真实循环；payload 带 analyses，
    投影出 insight 块，且答案基于真实计算不被拒答。"""
    domain_id, _onto, aliases = _seed_golden_domain()
    script = [
        ToolTurn([("select_skill", {"skill": "query"})]),
        ToolTurn([("run_sql", {"sql": "SELECT region, amount FROM t"})]),
        ToolTurn([("analyze_result", {})]),
        FinalTurn("分析显示金额分布存在离群。"),
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
        if name == "run_sql":
            return (
                {"executed": True, "sql": args.get("sql"),
                 "columns": [{"key": "region"}, {"key": "amount"}],
                 "rows": [{"region": "华东", "amount": a} for a in [10, 11, 9, 10, 12, 300]]},
                "返回 6 行", False,
            )
        return ({"error": f"unexpected {name}"}, "", True)

    service._dispatch_agent_tool = fake_dispatch  # type: ignore[assignment]
    try:
        with SessionLocal() as db:
            payload = asyncio.run(
                service.ask(db, domain_id=domain_id, question="各区域金额有没有异常",
                            principal_role="publisher")
            )
    finally:
        c.AsyncOpenAI = orig  # type: ignore[assignment]

    assert payload.get("grounding_refused") is not True, payload.get("answer")
    assert payload["skill"] == "query"
    assert payload["analyses"] and payload["analyses"][0]["total_outliers"] >= 1

    types = [b["type"] for b in answer_to_blocks(payload)]
    assert "insight" in types
    if "table" in types:  # 分析块紧跟结果表
        assert types.index("insight") > types.index("table")


def test_analyze_result_trend_and_jumps():
    """P5+：给 order_by 时算趋势方向 + 突变点（3×中位步长稳健阈值）。"""
    dr = {"columns": [{"key": "month"}, {"key": "gmv"}],
          "rows": [{"month": m, "gmv": g} for m, g in
                   [("01", 100), ("02", 110), ("03", 120), ("04", 130), ("05", 400)]]}
    r, _s, e = svc._dispatch_analyze_result({"order_by": "month"}, dr, [])
    assert e is False
    col = r["analysis"]["columns"][0]
    assert col["trend"]["direction"] == "up"
    assert col["jumps"] and col["jumps"][0]["at"] == "05"
    assert r["analysis"]["ordered_by"] == "month" and r["analysis"]["total_jumps"] >= 1
    # 无 order_by → 不算趋势
    r2, _s2, _e2 = svc._dispatch_analyze_result({}, dr, [])
    assert "trend" not in r2["analysis"]["columns"][0]


def test_analyze_result_no_false_jumps_on_stable():
    dr = {"columns": [{"key": "m"}, {"key": "v"}],
          "rows": [{"m": str(i), "v": 100 + (i % 2)} for i in range(6)]}
    r, _s, _e = svc._dispatch_analyze_result({"order_by": "m"}, dr, [])
    assert not r["analysis"]["columns"][0].get("jumps")


def test_live_task_state_best_effort(client, monkeypatch):
    """Airflow 实时态回读：读得到→surface live_state；读不到/无批次→None（退制品态）。"""
    import app.api.warehouse as wh
    import app.connectors.airflow as af

    monkeypatch.setattr(wh, "_receipt_batches", lambda db, aid: [{"dag_id": "d", "dag_run_id": "r"}])

    class _FakeAF:
        def __init__(self, *a, **k):
            pass

        def get_dag_run(self, d, r):
            return {"state": "running"}

        def run_url(self, d, r):
            return "http://x/run"

        def close(self):
            pass

    monkeypatch.setattr(af, "AirflowClient", _FakeAF)
    monkeypatch.setattr(
        svc.settings_service, "get_airflow_runtime",
        lambda db: SimpleNamespace(endpoint="e", username=None, password=None, token=None, api_version="v2"),
    )
    with SessionLocal() as db:
        live = svc._live_task_state(db, SimpleNamespace(id="a1"))
    assert live and live["live_state"] == "running" and live["terminal"] is False
    assert live["run_url"] == "http://x/run"

    monkeypatch.setattr(wh, "_receipt_batches", lambda db, aid: [])
    with SessionLocal() as db:
        assert svc._live_task_state(db, SimpleNamespace(id="a2")) is None


# ---------------- P3.1：显式偏好记忆（propose_preference / 本域约定） ----------------


def test_propose_preference_is_write_free():
    r, _s, e = svc._dispatch_propose_preference(domain_id="d1", args={"text": "成交额默认含税口径"})
    assert e is False and r["kind"] == "preference" and r["domain_id"] == "d1"
    _r2, _s2, e2 = svc._dispatch_propose_preference(domain_id="d1", args={"text": "  "})
    assert e2 is True


def test_record_domain_preference_and_card(client):
    domain_id, _onto, _aliases = _seed_golden_domain()
    with SessionLocal() as db:
        svc.record_domain_preference(db, domain_id, "成交额默认含税口径")
        svc.record_domain_preference(db, domain_id, "成交额默认含税口径")  # 幂等
        svc.record_domain_preference(db, domain_id, "华东含上海")
        from app.models.chat_bi import ChatBiDomainMemory
        n = db.query(ChatBiDomainMemory).filter_by(domain_id=domain_id, ref_kind="preference").count()
        card = svc.build_domain_memory_card(db, domain_id)
    assert n == 2  # 幂等：重复文本不新增
    assert "本域约定" in card and "成交额默认含税口径" in card and "华东含上海" in card


def test_preference_flow_stays_read_only(client):
    """端到端：propose_preference 穿真实循环出提案块，但 ask() 不写任何本域约定（只读边界）。"""
    from app.models.chat_bi import ChatBiDomainMemory

    domain_id, _onto, aliases = _seed_golden_domain()

    def _pref_count() -> int:
        with SessionLocal() as db:
            return db.query(ChatBiDomainMemory).filter_by(
                domain_id=domain_id, ref_kind="preference").count()

    before = _pref_count()
    script = [
        ToolTurn([("propose_preference", {"text": "成交额默认含税口径"})]),
        FinalTurn("好的，需要你确认后我才记住这条约定。"),
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
                service.ask(db, domain_id=domain_id, question="以后成交额都按含税口径", principal_role="publisher")
            )
    finally:
        c.AsyncOpenAI = orig  # type: ignore[assignment]

    assert payload.get("grounding_refused") is not True, payload.get("answer")
    assert payload["preference_proposals"] and payload["preference_proposals"][0]["text"] == "成交额默认含税口径"
    assert "preference_proposal" in [b["type"] for b in answer_to_blocks(payload)]
    assert _pref_count() == before  # 只读不变式：提案不落库


def test_remember_preference_endpoint(client, admin_headers):
    domain_id, _onto, _aliases = _seed_golden_domain()
    resp = client.post(
        "/api/chat-bi/domain-memory/preferences",
        json={"domain_id": domain_id, "text": "华东含上海"},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["remembered"] is True
    bad = client.post(
        "/api/chat-bi/domain-memory/preferences",
        json={"domain_id": "nope", "text": "x"}, headers=admin_headers,
    )
    assert bad.status_code == 400
