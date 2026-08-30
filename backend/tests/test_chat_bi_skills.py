"""Data Agent 技能层单测（V3 S1）。

覆盖：技能注册表、`select_skill` 的 overlay 叠加与工具解锁、`render_chart` 的接地校验
（x/y 必须是真实结果列、图型枚举、无数据即拒）。这些是「只解锁不收窄 + 图表不臆造列」
两条不变式的回归面。

V5.1: 新增 ReAct 模式的思考提取测试。
"""

from __future__ import annotations

import asyncio
import json
from uuid import uuid4
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
    assert set(SKILLS) == {"overview", "query", "lineage", "create", "task", "onboard", "ops"}
    assert SKILLS["query"].extra_tool_names == (
        "update_plan", "scout_query", "analyze_result", "render_chart",
        "propose_panel", "propose_dashboard",
    )
    assert SKILLS["overview"].extra_tool_names == ()
    assert SKILLS["lineage"].extra_tool_names == ("get_lineage", "list_datasets")
    assert SKILLS["create"].extra_tool_names == (
        "propose_draft", "propose_expression", "lint_against_standard",
    )
    assert SKILLS["task"].extra_tool_names == (
        "get_task_options", "propose_action", "propose_pipeline", "get_task_status",
        "lint_against_standard", "list_datasets",
    )
    assert SKILLS["onboard"].extra_tool_names == (
        "list_onboarding_targets", "propose_datasource", "propose_ontology_draft",
    )
    assert SKILLS["ops"].extra_tool_names == (
        "get_landing", "get_ops_record", "get_task_status", "get_lineage",
        "list_datasets", "lint_against_standard",
    )
    assert "overview" in skill_choices_text() and "query" in skill_choices_text()


def test_create_skill_unlocks_lint_without_prompt_governance_card():
    """治理由后端闸门执行；技能只给简短工具流程，不叠加治理规约全文。"""
    create = SKILLS["create"]
    assert create.attach_governance is False
    tools = {t["function"]["name"] for t in c._tools_for_skill(create)}
    assert "lint_against_standard" in tools
    messages = [{"role": "system", "content": "BASE"}]
    skill, _result, _summary, is_error = svc._apply_select_skill(
        {"skill": "create"}, messages, "BASE", "【治理规约】命名 snake_case"
    )
    assert is_error is False and skill.name == "create"
    assert "【治理规约】" not in messages[0]["content"]
    # V5: overlay 更详细，放宽长度限制
    assert len(messages[0]["content"]) < 1200  # 原 500 太严格


def test_safe_skill_selection_keeps_minimal_system_prompt():
    messages = [{"role": "system", "content": c._MINIMAL_AGENT_SYSTEM_PROMPT}]
    svc._apply_select_skill(
        {"skill": "task"}, messages, c._MINIMAL_AGENT_SYSTEM_PROMPT,
        "ignored", apply_overlay=False,
    )
    assert messages[0]["content"] == c._MINIMAL_AGENT_SYSTEM_PROMPT


def test_tools_only_unlock_never_shrink():
    """V5 改：真收窄而非只解锁。每个 skill 有独立白名单，工具集不再 ⊇ 基础集。"""
    # 默认工具集（未选 skill）
    default_tools = {t["function"]["name"] for t in c._tools_for_skill(None)}
    assert "select_skill" in default_tools
    assert "render_chart" not in default_tools

    # overview skill - 只有检索和概览工具
    overview_tools = {t["function"]["name"] for t in c._tools_for_skill(SKILLS["overview"])}
    assert "search_objects" in overview_tools
    assert "get_domain_overview" in overview_tools
    assert "select_skill" in overview_tools
    assert "render_chart" not in overview_tools  # 不解锁图表
    assert "run_sql" not in overview_tools  # 不需要取数

    # query skill - 解锁分析和图表工具
    query_tools = {t["function"]["name"] for t in c._tools_for_skill(SKILLS["query"])}
    assert "search_objects" in query_tools
    assert "run_sql" in query_tools
    assert "render_chart" in query_tools
    assert "analyze_result" in query_tools
    assert "get_domain_overview" not in query_tools  # query 不需要 overview 工具


def test_select_skill_appends_overlay_and_unlocks():
    messages = [{"role": "system", "content": "BASE"}]
    skill, result, summary, is_error = svc._apply_select_skill(
        {"skill": "query"}, messages, "BASE"
    )
    assert is_error is False
    assert skill is not None and skill.name == "query"
    assert messages[0]["content"].startswith("BASE\n\n")
    assert "【取数分析模式】" in messages[0]["content"]  # V5: 更详细的 overlay
    # V5: tools_unlocked 含义改为"该 skill 特有的工具"，不再是全量工具集
    assert result["tools_unlocked"] == [
        "update_plan", "scout_query", "analyze_result", "render_chart",
        "propose_panel", "propose_dashboard",
    ]


def test_select_skill_reselect_replaces_not_stacks():
    """重复选取以最后一次为准，overlay 不叠加。"""
    messages = [{"role": "system", "content": "BASE"}]
    svc._apply_select_skill({"skill": "query"}, messages, "BASE")
    svc._apply_select_skill({"skill": "overview"}, messages, "BASE")
    assert messages[0]["content"].count("BASE") == 1
    assert "【域概览模式】" in messages[0]["content"]  # V5: 更详细的 overlay
    assert "【取数分析模式】" not in messages[0]["content"]


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

    def fake_dispatch(db, *, domain_ids, ontology_ids, name, args, principal_role=None, conversation_id=None):
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
                service.ask(db, domain_ids=[domain_id], question="各区域销售额", principal_role="publisher")
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
                service.ask(db, domain_ids=[domain_id], question="订单的血缘", principal_role="publisher")
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
    r, _s, e = svc._dispatch_propose_draft(
        domain_id="d",
        args={"display_name": "复购率", "logic_type": "tag", "description": "复购客户数/总客户数"},
    )
    assert e is False
    assert r["create_payload"]["name"]  # 中文名派生占位标识符，非空


def test_propose_draft_requires_description():
    """口径说明是提案里唯一承载口径含义的字段，缺了它就是「有名字没口径」。

    实测的退化路径：propose_expression 连挂几轮 → 回退 propose_draft 只带名字 →
    「is_group=0 的分组数量」这层语义在提案里彻底消失，用户点确认建出一条空口径。
    """
    r, _s, e = svc._dispatch_propose_draft(
        domain_id="d", args={"display_name": "活跃客户分组数", "logic_type": "metric"}
    )
    assert e is True
    assert "description" in r["error"]


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


def test_dispatch_lint_compliant_and_kopi_proposal_reports_nothing_checked():
    """无 target_table 的 spec 一条规则都没跑过——不能回「合规」。

    Spec 层可校验的只有物理标识符命名（governance/lint.lint_spec）。口径提案没有物理
    表名，旧实现回 compliant=True，被转述成「已通过治理规范校验」——一个空洞的通过。
    """
    with SessionLocal() as db:
        clean, _s, _e = svc._dispatch_lint(
            db, args={"spec": {"target_table": "dim_customer"}}
        )
        kopi, kopi_summary, _e2 = svc._dispatch_lint(
            db, args={"spec": {"display_name": "复购率"}}
        )
    assert clean["compliant"] is True and clean["violations"] == []
    assert clean["checked_rules"]  # 真查了命名条款
    assert kopi["compliant"] is None  # 既不是合规也不是违规：没查
    assert kopi["checked_rules"] == []
    assert "没有校验任何条款" in kopi["note"]
    assert "无可校验条款" in kopi_summary


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
                service.ask(db, domain_ids=[domain_id], question="建个复购率指标", principal_role="publisher")
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
    from app.models import DataSource

    with SessionLocal() as db:
        old_defaults = [
            row.id for row in db.query(DataSource).filter(
                DataSource.is_default_warehouse.is_(True)
            ).all()
        ]
        db.query(DataSource).filter(DataSource.is_default_warehouse.is_(True)).update(
            {DataSource.is_default_warehouse: False}, synchronize_session=False
        )
        db.add(DataSource(
            id="ds-action-doris", name="默认 Doris", kind="doris", purpose="warehouse",
            is_default_warehouse=True, enabled=True, status="ok", dsn_secret_ref="ref://doris",
        ))
        db.commit()
        r, _summary, is_error = svc._dispatch_propose_action(
            db,
            ontology_id="onto1", domain_id="dom1",
            args={"kind": "materialize", "intent": "把客户主数据物化到数仓",
                  "context": {"target_datasource_id": "ds-action-doris",
                              "target_database": "dw", "selected_targets": ["customer"]}},
        )
        db.query(DataSource).filter(DataSource.id == "ds-action-doris").delete()
        if old_defaults:
            db.query(DataSource).filter(DataSource.id.in_(old_defaults)).update(
                {DataSource.is_default_warehouse: True}, synchronize_session=False
            )
        db.commit()
    assert is_error is False
    assert r["kind"] == "materialize"
    dp = r["draft_payload"]
    assert dp["kind"] == "materialize" and dp["ontology_id"] == "onto1"
    assert dp["intent"] == "把客户主数据物化到数仓"
    assert dp["context"]["target_database"] == "dw"
    assert dp["context"]["selected_targets"] == ["customer"]


def test_propose_action_rejects_bad_kind_and_missing_intent():
    with SessionLocal() as db:
        # cluster 不在数据任务白名单（归基建，不由聊天 agent 提）
        _r, _s, e1 = svc._dispatch_propose_action(
            db, ontology_id="o", domain_id="d", args={"kind": "cluster", "intent": "x"}
        )
        assert e1 is True
        _r2, _s2, e2 = svc._dispatch_propose_action(
            db, ontology_id="o", domain_id="d", args={"kind": "materialize"}
        )
    assert e2 is True  # 缺 intent


def test_propose_action_rejects_missing_required_context(client):
    """物化缺 target_datasource_id：当场判错并给出真实候选，不放一份点了必 400 的提案出去。

    此前不校验，提案照发，用户点「去校验并执行」才在 MaterializeDrafter 里抛
    「缺少必要上下文」——错误被推迟到了按钮之后。
    """
    from app.models import DataSource

    with SessionLocal() as db:
        db.query(DataSource).filter(DataSource.is_default_warehouse.is_(True)).update(
            {DataSource.is_default_warehouse: False}, synchronize_session=False
        )
        db.add(DataSource(
            id="ds-hive", name="生产 Doris", kind="doris", purpose="warehouse",
            is_default_warehouse=True, status="ok", dsn_secret_ref="mysql://doris",
        ))
        db.commit()
        r, summary, is_error = svc._dispatch_propose_action(
            db,
            ontology_id="onto1", domain_id="dom1",
            args={"kind": "materialize", "intent": "把客户主数据物化到数仓"},
        )
    assert is_error is True
    assert r["missing"] == ["target_datasource_id", "target_database"]
    assert "target_datasource_id" in summary and "target_database" in summary
    # 「怎么补」不能只说缺了什么：附上真实候选，模型才能据此发一张能选的表单。
    options = r["target_datasource_id_options"]
    assert {"id": "ds-hive", "name": "生产 Doris", "kind": "doris", "status": "ok"} in options
    # 凭据不出现在候选里
    assert all("dsn" not in k for o in options for k in o)
    with SessionLocal() as db:
        db.query(DataSource).filter(DataSource.id == "ds-hive").delete()
        db.commit()


def test_propose_action_requires_explicit_sync_endpoints_and_transform_doris():
    """sync 端点与 transform 默认 Doris 都不可由模型猜测。"""
    with SessionLocal() as db:
        result, _summary, is_error = svc._dispatch_propose_action(
            db, ontology_id="onto1", domain_id="dom1",
            args={"kind": "sync", "intent": "把订单表搬到数仓"},
        )
        assert is_error is True
        # 落点库不在必填里：同步恒写 ODS，由 Drafter 定，不该要模型/用户给。
        assert set(result["missing"]) == {
            "source_datasource_id", "target_datasource_id",
        }
        transform, _s, is_error = svc._dispatch_propose_action(
            db, ontology_id="onto1", domain_id="dom1",
            args={"kind": "transform", "intent": "清洗订单"},
        )
        assert is_error is True
        assert transform["missing"] == ["target_datasource_id"]


def test_sync_proposal_requires_conversation_confirmations(
    client, admin_headers, monkeypatch
):
    """识别出 sync 任务不等于需求已确认；通过后仍校验真实业务源与默认 Doris。"""
    from uuid import uuid4

    from app.models import DataSource, ObjectType
    from app.models.chat_bi import ChatBiConversation
    from app.services.materialize_preflight import PreflightReport
    from app.services.chat_bi_ledger import record_decision

    monkeypatch.setattr(
        "app.services.materialize_preflight.run_preflight",
        lambda *args, **kwargs: PreflightReport(),
    )

    domain_id, ontology_id, aliases = _seed_golden_domain()
    source_id = f"source-confirm-{uuid4().hex[:8]}"
    target_id = f"doris-confirm-{uuid4().hex[:8]}"
    with SessionLocal() as db:
        order = db.get(ObjectType, aliases["@order"])
        order.source_ref = "urn:li:dataset:(urn:li:dataPlatform:postgres,erp.public.order,PROD)"
        old_defaults = [
            row.id for row in db.query(DataSource).filter(
                DataSource.is_default_warehouse.is_(True)
            ).all()
        ]
        db.query(DataSource).filter(DataSource.is_default_warehouse.is_(True)).update(
            {DataSource.is_default_warehouse: False}, synchronize_session=False
        )
        db.add_all([
            DataSource(
                id=source_id, name="ERP PG", kind="postgres", purpose="business_source",
                enabled=True, status="ok", catalog_name="erp", dsn_secret_ref="postgresql://reader@db/erp",
            ),
            DataSource(
                id=target_id, name="默认 Doris", kind="doris", purpose="warehouse",
                is_default_warehouse=True, enabled=True, status="ok", dsn_secret_ref="ref://doris",
            ),
        ])
        conv = ChatBiConversation(title="同步闭环门禁")
        db.add(conv)
        db.commit()
        result, _summary, is_error = svc._dispatch_propose_action(
            db, ontology_id=ontology_id, domain_id=domain_id, conversation_id=conv.id,
            args={"kind": "sync", "intent": "同步订单到数仓", "context": {
                "object_type": "order", "source_datasource_id": source_id,
                "target_datasource_id": target_id, "target_ods_database": "ods", "mode": "full",
            }},
        )
        assert is_error is True
        assert result["missing_confirmations"] == ["requirement", "ontology", "data"]

        confirmation_id = "sync-confirm-1"
        for node in ("requirement", "ontology", "data"):
            record_decision(
                db, conversation_id=conv.id, node=node,
                stage=f"task_{node}_confirm", outcome="accepted",
                chosen={
                    "task_confirmation_id": confirmation_id,
                    **({"task_requirement": "同步已确认的订单到数仓"} if node == "requirement" else {}),
                },
            )
        proposal, _summary, is_error = svc._dispatch_propose_action(
            db, ontology_id=ontology_id, domain_id=domain_id, conversation_id=conv.id,
            args={"kind": "sync", "intent": "同步订单到数仓", "context": {
                "task_confirmation_id": confirmation_id,
                "object_type": "order", "source_datasource_id": source_id,
                "target_datasource_id": target_id, "mode": "full",
            }},
        )
        assert is_error is False
        assert proposal["confirmation_id"] == confirmation_id
        conversation_id = conv.id

    response = client.post(
        "/api/agents/draft-confirmed",
        headers=admin_headers,
        json={
            "conversation_id": conversation_id,
            "confirmation_id": confirmation_id,
            "kind": "sync",
            "intent": "同步订单到数仓",
            "ontology_id": ontology_id,
            "context": {
                "object_type": "order",
                "source_datasource_id": source_id,
                "target_datasource_id": target_id,
                "target_ods_database": "ods",
                "target_ods_table": "caller_defined",
                "mode": "full",
            },
        },
    )
    assert response.status_code == 200, response.text
    artifact = response.json()
    assert artifact["status"] == "validated"
    assert artifact["intent"] == "同步已确认的订单到数仓"
    assert artifact["validation_report"]["dry_run"]["target_ods_table"].endswith("_order")
    assert artifact["spec"]["target_ods_table"].endswith("_order")
    assert artifact["spec"]["target_ods_table"] != "caller_defined"

    # 闭环按任务分开：前三环在制品还不存在时就确认了，只带表单的 confirmation_id。
    # draft-confirmed 必须把它落到 (会话, 制品) 关联上，否则这条任务的卡片只剩后三环，
    # 明明逐环确认过的需求/本体/数据在界面上恒灰。
    closure = client.get(
        f"/api/chat-bi/conversations/{conversation_id}/closure", headers=admin_headers
    ).json()
    assert [t["artifact_id"] for t in closure["tasks"]] == [artifact["id"]]
    task = closure["tasks"][0]
    assert task["confirmation_id"] == confirmation_id
    assert task["reached_count"] == 3
    assert [n["node"] for n in task["nodes"] if n["reached"]] == [
        "requirement",
        "ontology",
        "data",
    ]

    with SessionLocal() as db:
        db.query(DataSource).filter(DataSource.id.in_([source_id, target_id])).delete(
            synchronize_session=False
        )
        if old_defaults:
            db.query(DataSource).filter(DataSource.id.in_(old_defaults)).update(
                {DataSource.is_default_warehouse: True}, synchronize_session=False
            )
        db.commit()


def test_confirmed_task_rejects_ontology_outside_conversation_scope(
    client, admin_headers
):
    """直达草稿接口不能用前端 ontology_id 越过会话绑定的数据域。"""
    from app.models.chat_bi import ChatBiConversation

    domain_a, _ontology_a, _aliases_a = _seed_golden_domain()
    _domain_b, ontology_b, _aliases_b = _seed_golden_domain()
    with SessionLocal() as db:
        conv = ChatBiConversation(title="作用域门禁")
        conv.set_domain_ids([domain_a])
        db.add(conv)
        db.commit()
        conversation_id = conv.id

    response = client.post(
        "/api/agents/draft-confirmed",
        headers=admin_headers,
        json={
            "conversation_id": conversation_id,
            "confirmation_id": "foreign-ontology",
            "kind": "materialize",
            "intent": "物化别域对象",
            "ontology_id": ontology_b,
            "context": {},
        },
    )
    assert response.status_code == 409, response.text
    assert "不属于当前会话的数据域作用域" in response.json()["detail"]


def test_all_write_task_proposals_require_three_step_confirmation():
    """物化/同步/加工/聚合都不能把“识别出任务”当作“用户已确认”。"""
    from app.models.chat_bi import ChatBiConversation

    with SessionLocal() as db:
        conv = ChatBiConversation(title="四类任务闭环门禁")
        db.add(conv)
        db.commit()
        for kind in ("materialize", "sync", "transform", "metric"):
            result, _summary, is_error = svc._dispatch_propose_action(
                db,
                ontology_id="onto1",
                domain_id="dom1",
                conversation_id=conv.id,
                args={"kind": kind, "intent": f"新建 {kind} 任务", "context": {}},
            )
            assert is_error is True
            assert result["missing_confirmations"] == ["requirement", "ontology", "data"]


def test_materialize_required_context_matches_standard():
    """物化的必填 context 与规约的必填 Spec 字段一致——提案前置校验与 Validation Gate
    守同一份判据，不得一处改了另一处没跟上。

    其余类型不做此断言：它们的必填 Spec 字段（sync 的 source/target、metric 的
    metric_name）由 Drafter 从本体推导，本就不是调用方要给的 context 键。
    """
    from app.agents import registry
    from app.governance import active_standard

    per_artifact = active_standard().required_metadata.per_artifact
    assert set(registry.get_drafter("materialize").required_context) == set(
        per_artifact["materialize"]
    )


def test_task_skill_unlocks_action_tools():
    task = SKILLS["task"]
    assert task.attach_governance is False
    tools = {t["function"]["name"] for t in c._tools_for_skill(task)}
    # V5: 真收窄，task skill 只有任务相关工具，不包含全部基础工具
    assert {"propose_action", "get_task_status", "lint_against_standard"} <= tools
    assert "search_objects" in tools  # 基础检索保留
    assert "get_object" in tools
    assert "select_skill" in tools
    # 但不包含不相关的工具
    assert "render_chart" not in tools
    assert "get_lineage" not in tools


def test_existing_conversation_uses_its_own_domain_scope(client, admin_headers):
    """填表期间页面作用域变化时，续聊仍使用会话创建时的数据域。"""
    domain_a, _onto_a, _aliases_a = _seed_golden_domain()
    domain_b, _onto_b, _aliases_b = _seed_golden_domain()
    with SessionLocal() as db:
        conv = svc.create_conversation(db, domain_ids=[domain_a], title="作用域稳定")
    response = client.post(
        "/api/chat-bi/ask",
        headers=admin_headers,
        json={
            "domain_ids": [domain_b],  # 模拟页面筛选在表单填写期间发生变化
            "conversation_id": conv["id"],
            "question": "继续确认数据",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["domain_ids"] == [domain_a]


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


def test_normal_agent_request_uses_compact_tool_schemas(client):
    """正常路径也只发送工具名和参数结构，不携带长自然语言工具说明。"""
    domain_id, _onto, aliases = _seed_golden_domain()
    inner = _StubCompletions([FinalTurn("你好")], aliases)
    calls: list[dict] = []
    original_create = inner.create

    async def capture(**kwargs):
        calls.append(kwargs)
        return await original_create(**kwargs)

    inner.create = capture  # type: ignore[assignment]
    orig = c.AsyncOpenAI
    c.AsyncOpenAI = lambda **_k: _StubClient(inner)  # type: ignore[assignment]
    service = ChatBiService()
    service.settings_service = SimpleNamespace(  # type: ignore[assignment]
        get_llm_runtime=lambda _db: SimpleNamespace(
            api_key="stub-key", api_base_url="http://stub", model="stub-model"
        )
    )
    try:
        with SessionLocal() as db:
            asyncio.run(service.ask(db, domain_ids=[domain_id], question="你好"))
    finally:
        c.AsyncOpenAI = orig  # type: ignore[assignment]

    assert calls
    assert calls[0]["messages"][0]["content"].startswith("你是企业数据助手")
    for tool in calls[0].get("tools") or []:
        fn = tool["function"]
        assert "description" not in fn
        assert "description" not in json.dumps(fn.get("parameters") or {})


def test_flagged_prompt_retries_with_compact_tools_and_no_skill_overlay(client):
    """上游误判 prompt 后，第二次请求要真的精简 messages + tools，且选技能后不恢复长 overlay。"""
    domain_id, _onto, aliases = _seed_golden_domain()
    script = [
        ToolTurn([("select_skill", {"skill": "task"})]),
        ToolTurn([("request_form", {
            "title": "确认同步任务", "task_kind": "sync", "intent": "同步订单到数仓",
        })]),
    ]
    inner = _StubCompletions(script, aliases)

    class _FlagOnce:
        def __init__(self):
            self.calls: list[dict] = []

        async def create(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                raise ValueError(
                    "Invalid prompt: your prompt was flagged as potentially violating our usage policy"
                )
            return await inner.create(**kwargs)

    completions = _FlagOnce()
    orig = c.AsyncOpenAI
    c.AsyncOpenAI = lambda **_k: _StubClient(completions)  # type: ignore[assignment]
    service = ChatBiService()
    service.settings_service = SimpleNamespace(  # type: ignore[assignment]
        get_llm_runtime=lambda _db: SimpleNamespace(
            api_key="stub-key", api_base_url="http://stub", model="stub-model"
        )
    )
    try:
        with SessionLocal() as db:
            payload = asyncio.run(
                service.ask(
                    db, domain_ids=[domain_id], question="同步订单到数仓",
                    principal_role="publisher",
                )
            )
    finally:
        c.AsyncOpenAI = orig  # type: ignore[assignment]

    assert payload["form_request"]["confirmation_id"]
    assert len(completions.calls) >= 3  # 被标记 → 精简后选技能 → request_form
    for call in completions.calls[1:]:
        system = call["messages"][0]["content"]
        assert system == c._MINIMAL_AGENT_SYSTEM_PROMPT
        assert "【数据任务】" not in system
        for tool in call.get("tools") or []:
            fn = tool["function"]
            assert "description" not in fn
            assert "description" not in json.dumps(fn.get("parameters") or {})


def test_task_skill_flow_stays_read_only(client):
    """端到端：识别物化任务后先出确认表单，不得跳过需求/本体/数据直接提案。"""
    from app.models.agent import GovernanceArtifact

    domain_id, _onto, aliases = _seed_golden_domain()

    def _count() -> int:
        with SessionLocal() as db:
            return db.query(GovernanceArtifact).count()

    before = _count()
    script = [
        ToolTurn([("select_skill", {"skill": "task"})]),
        ToolTurn([("request_form", {
            "title": "确认物化任务",
            "task_kind": "materialize",
            "intent": "把客户主数据物化落库",
        })]),
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
                service.ask(db, domain_ids=[domain_id], question="把客户主数据物化落库",
                            principal_role="publisher")
            )
    finally:
        c.AsyncOpenAI = orig  # type: ignore[assignment]

    assert payload.get("grounding_refused") is not True, payload.get("answer")
    assert payload["form_request"]
    assert payload["form_request"]["confirmation_id"]
    assert [s["node"] for s in payload["form_request"]["confirmation_steps"]] == [
        "requirement", "ontology", "data", "plan", "execute", "result"
    ]
    assert "form" in [b["type"] for b in answer_to_blocks(payload)]
    assert not payload.get("action_proposals")
    assert _count() == before


# ---------------- P1：建数任务可选项目录（get_task_options） ----------------


def test_task_options_materialize_lists_real_datasources_and_entities(client):
    """物化可选项：数据源 + 待物化实体（带契约 id / 分层 / 分区键 / 装载方式 / 调度）
    + 装载方式 + 调度频率预置。

    这是建数表单能长出下拉框的前提：此前模型没有任何工具读得到这些，request_form 的
    「候选项须来自真实实体」永远无法满足，只能退化成文本框。
    """
    from app.models import DataSource

    _domain_id, onto_id, _aliases = _seed_golden_domain()
    with SessionLocal() as db:
        db.query(DataSource).filter(DataSource.is_default_warehouse.is_(True)).update(
            {DataSource.is_default_warehouse: False}, synchronize_session=False
        )
        db.add(DataSource(id="ds-dw", name="生产 Doris", kind="doris", purpose="warehouse",
                          is_default_warehouse=True, status="ok",
                          dsn_secret_ref="ref://dw"))
        db.commit()
        try:
            # 契约由本体推导，先对齐一次
            from app.api.deps import materialization_contract_service as contracts_svc

            contracts_svc.sync(db, onto_id)
            r, summary, is_error = svc._dispatch_get_task_options(
                db, ontology_id=onto_id, args={"kind": "materialize"},
            )
        finally:
            # 用例间共用一个库：带 dsn 的源会被 _resolve_domain_data_source 选去当
            # run_sql 的执行源，留着会串到别的用例（取数用例就会连错库）。
            db.query(DataSource).filter(DataSource.id == "ds-dw").delete()
            db.commit()

    assert is_error is False
    target = next(d for d in r["datasources"] if d["id"] == "ds-dw")
    assert target["name"] == "生产 Doris"
    assert target["engine"] == "doris"
    assert target["writable"] is True and target["executable"] is True
    assert r["engine"] == "doris"
    assert r["entities"] and all(
        {"contract_id", "entity", "display_name", "layer", "derived_only"} <= set(e)
        for e in r["entities"]
    )
    # 物化只建结构；装载/分区/调度属于同步，不能再误导模型。
    assert "load_strategies" not in r
    assert "partition_key_candidates" not in r
    assert "cron_presets" not in r
    assert "个数据源" in summary


def test_task_options_caps_and_filters_entities(client):
    """候选按 search_* 的既有约定给 {total/returned/truncated}，并支持关键词收窄——
    一个 700+ 对象的域整份倒进上下文既挤爆预算也没人读。"""
    _domain_id, onto_id, _aliases = _seed_golden_domain()
    with SessionLocal() as db:
        from app.api.deps import materialization_contract_service as contracts_svc

        contracts_svc.sync(db, onto_id)
        r, _s, is_error = svc._dispatch_get_task_options(
            db, ontology_id=onto_id, args={"kind": "materialize", "keyword": "订单"},
        )
    assert is_error is False
    assert r["returned"] == len(r["entities"]) <= r["total_entities"]
    # 过滤掉了别的实体，但订单本身留下了——否则这条断言是空真的
    assert r["entities"] and r["returned"] < r["total_entities"]
    assert all("订单" in (e["display_name"] or "") or "order" in e["entity"]
               for e in r["entities"])


def test_task_options_sync_excludes_objects_without_source(client):
    """同步候选只留有 source_ref 的对象——没有源表定位不了，选了也只会在起草时报错。"""
    _domain_id, onto_id, _aliases = _seed_golden_domain()
    with SessionLocal() as db:
        r, _s, is_error = svc._dispatch_get_task_options(
            db, ontology_id=onto_id, args={"kind": "sync"},
        )
    assert is_error is False
    assert r["context_key"] == "object_type"  # 键名对齐 SyncDrafter 认的 context 键
    assert all(o["source_table"] for o in r["objects"])


def test_task_options_transform_exposes_cleansing_vocabulary(client):
    """加工候选带上清洗规则词表：规则是闭集，词表外的需求会被 Drafter 静默丢掉，
    得让模型当场告诉用户做不了，而不是产出一个什么都不做的 ETL 任务。"""
    from app.agents.drafters.transform import SUPPORTED_CLEANSING_RULES

    _domain_id, onto_id, _aliases = _seed_golden_domain()
    with SessionLocal() as db:
        r, _s, is_error = svc._dispatch_get_task_options(
            db, ontology_id=onto_id, args={"kind": "transform"},
        )
    assert is_error is False
    assert r["context_key"] == "target_table"
    assert [x["rule"] for x in r["cleansing_rules"]] == [c for c, _d in SUPPORTED_CLEANSING_RULES]


def test_task_options_transform_keeps_derived_objects_without_own_ods(client):
    """派生对象的加工源来自定义中的多个上游，不要求它自己有 ODS projection。"""
    from app.models import ObjectType

    _domain_id, onto_id, _aliases = _seed_golden_domain()
    name = f"customer_rollup_{uuid4().hex[:8]}"
    with SessionLocal() as db:
        db.add(
            ObjectType(
                ontology_id=onto_id,
                name=name,
                display_name="客户汇总",
                table_role="business_object",
                source_ref=f"derived:{onto_id}:{name}",
            )
        )
        db.commit()
        r, _s, is_error = svc._dispatch_get_task_options(
            db, ontology_id=onto_id, args={"kind": "transform"},
        )

    assert is_error is False
    assert any(item["name"] == name for item in r["objects"])


def test_task_options_rejects_unknown_kind(client):
    with SessionLocal() as db:
        _r, _s, is_error = svc._dispatch_get_task_options(
            db, ontology_id="o", args={"kind": "cluster"},
        )
    assert is_error is True


def test_task_options_names_enter_ledger(client):
    """目录里的数据源名/库名/实体名要入账本，否则模型照着候选作答会被 F4 判成幻觉拒答。"""
    from app.services.agent_grounding import FactLedger

    ledger = FactLedger()
    svc._ledger_register(
        ledger,
        "get_task_options",
        {"datasources": [{"name": "数仓 Hive"}], "databases": ["dw"],
         "entities": [{"entity": "order", "display_name": "订单"}]},
        False,
    )
    assert ledger.has_entity_named("数仓 Hive")
    assert ledger.has_entity_named("dw")
    assert ledger.has_entity_named("订单")


def test_materialize_spec_carries_batch_cron():
    """整批调度提到 Spec 顶层：对话里「每天凌晨跑」说的是整批，不该要求先知道契约 id。"""
    from app.agents import registry

    spec = registry.get_drafter("materialize").draft(
        "把客户主数据物化到数仓",
        {"ontology_id": "onto1", "target_datasource_id": "ds-1", "refresh_cron": "0 2 * * *"},
    )
    assert spec["refresh_cron"] == "0 2 * * *"
    # 不给就是 None（不定时），不能凭空塞一个默认调度
    spec2 = registry.get_drafter("materialize").draft(
        "把客户主数据物化到数仓", {"ontology_id": "onto1", "target_datasource_id": "ds-1"}
    )
    assert spec2["refresh_cron"] is None


# ---------------- P1：跨轮任务记忆（会话 ↔ 任务关联） ----------------


def test_link_conversation_task_is_idempotent(client):
    from app.models.agent import ArtifactStatus, GovernanceArtifact

    domain_id, onto_id, _aliases = _seed_golden_domain()
    with SessionLocal() as db:
        conv = svc.create_conversation(db, domain_ids=[domain_id], title="t")
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


def test_link_conversation_task_backfills_confirmation_id(client):
    """幂等命中时补上缺失的 confirmation_id，但绝不覆盖已有的。

    关联可能先由别的路径建过（链推进、前端补登记）；不补，这条任务的前三环就永远
    归属不上、闭环卡上恒灰。而覆盖已有值等于把 A 表单的确认扣到 B 任务头上——
    宁可空着，也不能记错谁确认了什么。
    """
    from app.models.agent import ArtifactStatus, GovernanceArtifact
    from app.models.chat_bi import ChatBiConversationTask

    domain_id, onto_id, _aliases = _seed_golden_domain()
    with SessionLocal() as db:
        conv = svc.create_conversation(db, domain_ids=[domain_id], title="t")
        cid = conv["id"]
        a = GovernanceArtifact(
            kind="sync", name="同步客户", ontology_id=onto_id,
            intent="i", spec_json="{}", status=ArtifactStatus.DRAFTED.value,
        )
        db.add(a)
        db.commit()
        a_id = a.id

        svc.link_conversation_task(db, cid, a_id, kind="sync")
        svc.link_conversation_task(db, cid, a_id, kind="sync", confirmation_id="conf-1")
        svc.link_conversation_task(db, cid, a_id, kind="sync", confirmation_id="conf-2")

        rows = (
            db.query(ChatBiConversationTask)
            .filter(ChatBiConversationTask.conversation_id == cid)
            .all()
        )
        assert [r.confirmation_id for r in rows] == ["conf-1"]


def test_get_task_status_prefers_conversation_scope(client):
    """P1：给了 conversation_id 且未指定 artifact_id → 优先本会话催生的任务，
    而非整个本体的最近任务。"""
    from app.models.agent import ArtifactStatus, GovernanceArtifact

    domain_id, onto_id, _aliases = _seed_golden_domain()
    with SessionLocal() as db:
        conv = svc.create_conversation(db, domain_ids=[domain_id], title="t")
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
        conv = svc.create_conversation(db, domain_ids=[domain_id], title="t")
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

    def fake_dispatch(db, *, domain_ids, ontology_ids, name, args, principal_role=None, conversation_id=None):
        if name == "run_sql":
            return (
                {"executed": True, "sql": args.get("sql"),
                 "columns": [{"key": "region"}, {"key": "gmv"}],
                 "rows": [{"region": "华东", "gmv": 100}, {"region": "华北", "gmv": 80}]},
                "返回 2 行", False,
            )
        # update_plan 无需 db/上下文，走真实 dispatch
        return real_dispatch(db, domain_ids=domain_ids, ontology_ids=ontology_ids, name=name, args=args, principal_role=principal_role)

    service._dispatch_agent_tool = fake_dispatch  # type: ignore[assignment]
    try:
        with SessionLocal() as db:
            payload = asyncio.run(
                service.ask(db, domain_ids=[domain_id], question="探索各区域销售额有没有异常",
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
        svc.record_domain_memory(db, [domain_id], hit)
        svc.record_domain_memory(db, [domain_id], hit)  # 再命中一次 → 累加
        # 拒答不记
        svc.record_domain_memory(db, [domain_id],
            {"grounding_refused": True, "referenced_objects": [{"id": "o2", "display_name": "X"}]},
        )
        # mock 不记
        svc.record_domain_memory(db, [domain_id],
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
            svc.record_domain_memory(db, [domain_id], {"referenced_objects": [{"id": "hot", "display_name": "订单"}]})
        svc.record_domain_memory(db, [domain_id], {"referenced_objects": [{"id": "cold", "display_name": "冷门对象"}]})
        svc.record_domain_memory(db, [domain_id], {"referenced_logics": [{"id": "m", "display_name": "成交额"}]})
        card = svc.build_domain_memory_card(db, [domain_id], limit=8)
        empty = svc.build_domain_memory_card(db, ["no-such-domain"])
    assert "高频" in card
    assert "常用对象" in card and "订单" in card
    assert "常用口径" in card and "成交额" in card
    assert empty == ""  # 无记忆返回空串（不污染提示）


def test_domain_memory_injected_into_system_prompt(client):
    """端到端：先沉淀记忆，再问一次；系统提示 messages[0] 应含本域高频软提示。"""
    domain_id, _onto, aliases = _seed_golden_domain()
    with SessionLocal() as db:
        for _ in range(2):
            svc.record_domain_memory(db, [domain_id], {"referenced_objects": [{"id": "o1", "display_name": "客户档案"}]})

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
            asyncio.run(service.ask(db, domain_ids=[domain_id], question="随便问问", principal_role="publisher"))
    finally:
        c.AsyncOpenAI = orig  # type: ignore[assignment]

    assert "高频" in seen.get("system", "")
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

    def fake_dispatch(db, *, domain_ids, ontology_ids, name, args, principal_role=None, conversation_id=None):
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
                service.ask(db, domain_ids=[domain_id], question="各区域金额有没有异常",
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
        card = svc.build_domain_memory_card(db, [domain_id])
    assert n == 2  # 幂等：重复文本不新增
    assert "约定" in card and "成交额默认含税口径" in card and "华东含上海" in card


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
                service.ask(db, domain_ids=[domain_id], question="以后成交额都按含税口径", principal_role="publisher")
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


# ------------- 数据应用车道：面板/看板提案（query 技能） -------------


_OBJ_REF = [{"id": "obj-1", "name": "order", "display_name": "订单"}]


def test_propose_panel_builds_create_payload():
    r, summary, is_error = svc._dispatch_propose_app(
        kind="panel", domain_id="d1", question="各区域订单量",
        args={"title": "各区域订单量", "viz_type": "bar"},
        referenced_objects=_OBJ_REF,
    )
    assert is_error is False
    assert r["kind"] == "panel" and r["title"] == "各区域订单量"
    # 载荷是 generate-widget 的入参；口径不在这里带——由前端并上本条消息的 payload。
    assert r["create_payload"] == {
        "domain_id": "d1", "question": "各区域订单量", "widget_type": "bar", "name": "各区域订单量",
    }
    assert "caliber_decomposition" not in r["create_payload"]
    assert "面板" in summary


def test_propose_panel_refuses_without_hit_object():
    """没命中任何对象就提面板 = 生成一个空壳。当场判错，别等用户点了才在服务端抛。"""
    r, _s, is_error = svc._dispatch_propose_app(
        kind="panel", domain_id="d1", question="你能做什么",
        args={"title": "空壳", "viz_type": "bar"}, referenced_objects=[],
    )
    assert is_error is True and "主对象" in r["error"]


def test_propose_panel_rejects_bad_viz_and_missing_title():
    r1, _s1, e1 = svc._dispatch_propose_app(
        kind="panel", domain_id="d1", question="q",
        args={"title": "x", "viz_type": "sankey"}, referenced_objects=_OBJ_REF,
    )
    assert e1 is True and "sankey" in r1["error"]
    r2, _s2, e2 = svc._dispatch_propose_app(
        kind="panel", domain_id="d1", question="q",
        args={"viz_type": "bar"}, referenced_objects=_OBJ_REF,
    )
    assert e2 is True and "title" in r2["error"]


def test_propose_dashboard_builds_create_payload():
    r, _s, is_error = svc._dispatch_propose_app(
        kind="dashboard", domain_id="d1", question="渠道销售",
        args={"name": "渠道销售看板", "panel_title": "各渠道销售额", "viz_type": "bar"},
        referenced_objects=_OBJ_REF,
    )
    assert is_error is False
    assert r["kind"] == "dashboard" and r["title"] == "各渠道销售额"
    assert r["create_payload"]["app_type"] == "dashboard"
    assert r["create_payload"]["name"] == "渠道销售看板"


def test_propose_dashboard_defaults_panel_title_to_name():
    r, _s, e = svc._dispatch_propose_app(
        kind="dashboard", domain_id="d1", question="q",
        args={"name": "销售看板"}, referenced_objects=_OBJ_REF,
    )
    assert e is False and r["title"] == "销售看板" and r["viz_type"] == "bar"


def test_panel_flow_stays_read_only(client):
    """端到端：select_skill(query) → get_object → propose_panel 穿真实循环；
    payload 带提案、投影出 app_proposal 块，且 **ask() 不新建任何数据应用**。"""
    from app.models import DataApp, DataAppWidget

    domain_id, _onto, aliases = _seed_golden_domain()

    def _counts() -> tuple[int, int]:
        with SessionLocal() as db:
            return db.query(DataApp).count(), db.query(DataAppWidget).count()

    before = _counts()
    script = [
        ToolTurn([("select_skill", {"skill": "query"})]),
        ToolTurn([("get_object", {"object_id": "@order"})]),
        ToolTurn([("propose_panel", {"title": "订单量趋势", "viz_type": "bar"})]),
        FinalTurn("已为你拟好「订单量趋势」面板，点确认即可生成并加入看板。"),
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
                service.ask(db, domain_ids=[domain_id], question="订单量趋势做成图",
                            principal_role="publisher")
            )
    finally:
        c.AsyncOpenAI = orig  # type: ignore[assignment]

    assert payload.get("grounding_refused") is not True, payload.get("answer")
    assert payload["app_proposals"] and payload["app_proposals"][0]["title"] == "订单量趋势"
    assert "app_proposal" in [b["type"] for b in answer_to_blocks(payload)]
    assert _counts() == before  # 只读不变式：提案不建应用


# ------------- 接数据车道：数据源/本体草稿提案（onboard 技能） -------------


def test_list_onboarding_targets_reports_domains_and_sources():
    domain_id, _onto, _aliases = _seed_golden_domain()
    with SessionLocal() as db:
        r, summary, is_error = svc._dispatch_list_onboarding_targets(db)
    assert is_error is False
    ids = {d["domain_id"] for d in r["domains"]}
    assert domain_id in ids
    hit = next(d for d in r["domains"] if d["domain_id"] == domain_id)
    assert hit["has_published_ontology"] is True and hit["published_object_count"] >= 2
    assert isinstance(r["data_sources"], list)
    # 边界如实告知：采集不由本系统触发
    assert "DataHub" in r["datahub_note"]
    assert "域" in summary


def test_propose_datasource_drops_credentials():
    """凭据不进提案：模型硬塞 dsn/password 也会被丢掉，并如实回告哪些参数没被采纳。"""
    r, summary, is_error = svc._dispatch_propose_datasource(
        args={
            "name": "ERP 生产库（只读）", "kind": "mysql", "catalog_name": "erp",
            "dsn_secret_ref": "mysql://root:hunter2@10.0.0.1/erp",
            "password": "hunter2", "username": "root",
        }
    )
    assert is_error is False
    blob = json.dumps(r, ensure_ascii=False)
    assert "hunter2" not in blob and "root" not in blob
    assert r["create_payload"] == {"name": "ERP 生产库（只读）", "kind": "mysql", "catalog_name": "erp"}
    assert r["credentials_required"] is True
    assert set(r["dropped_args"]) == {"dsn_secret_ref", "password", "username"}
    assert "凭据待用户填" in summary


def test_propose_datasource_rejects_bad_kind_and_missing_name():
    r1, _s1, e1 = svc._dispatch_propose_datasource(args={"name": "x", "kind": "oracle"})
    assert e1 is True and "oracle" in r1["error"]
    r2, _s2, e2 = svc._dispatch_propose_datasource(args={"kind": "mysql"})
    assert e2 is True and "name" in r2["error"]


def test_propose_ontology_draft_builds_payload_and_flags_published():
    domain_id, _onto, _aliases = _seed_golden_domain()
    with SessionLocal() as db:
        r, summary, is_error = svc._dispatch_propose_ontology_draft(
            db, args={"domain_id": domain_id, "scope": "objects", "reason": "补几个对象"}
        )
    assert is_error is False
    assert r["create_payload"] == {"domain_id": domain_id, "scope": "objects"}
    # 该域已有发布本体 → 前端据此提示「重跑产生新草稿走合并，不是原地覆盖」
    assert r["has_published_ontology"] is True
    assert "业务对象" in summary


def test_propose_ontology_draft_rejects_unknown_domain_and_scope():
    with SessionLocal() as db:
        r1, _s1, e1 = svc._dispatch_propose_ontology_draft(db, args={"domain_id": "nope"})
        assert e1 is True and "不存在" in r1["error"]
        domain_id, _o, _a = _seed_golden_domain()
        r2, _s2, e2 = svc._dispatch_propose_ontology_draft(
            db, args={"domain_id": domain_id, "scope": "everything"}
        )
    assert e2 is True and "everything" in r2["error"]


def test_onboard_flow_stays_read_only(client):
    """端到端：select_skill(onboard) → list_onboarding_targets → propose_datasource
    穿真实循环；出 onboard_proposal 块，且 **ask() 不新建任何数据源**。"""
    from app.models import DataSource

    domain_id, _onto, aliases = _seed_golden_domain()

    def _ds_count() -> int:
        with SessionLocal() as db:
            return db.query(DataSource).count()

    before = _ds_count()
    script = [
        ToolTurn([("select_skill", {"skill": "onboard"})]),
        ToolTurn([("list_onboarding_targets", {})]),
        ToolTurn([("propose_datasource", {"name": "ERP 只读库", "kind": "mysql",
                                           "catalog_name": "erp"})]),
        FinalTurn("已拟好数据源登记提案，连接信息需要你自己填，我不经手凭据。"),
    ]
    completions = _StubCompletions(script, aliases)
    orig = c.AsyncOpenAI
    c.AsyncOpenAI = lambda **_k: _StubClient(completions)  # type: ignore[assignment]
    service = ChatBiService()
    service.settings_service = SimpleNamespace(  # type: ignore[assignment]
        get_llm_runtime=lambda _db: SimpleNamespace(
            api_key="stub-key", api_base_url="http://stub", model="stub-model"
        ),
        get_datahub_runtime=lambda _db: SimpleNamespace(gms_url=""),
    )
    service._resolve_domain_data_source = lambda _db: None  # type: ignore[assignment]
    try:
        with SessionLocal() as db:
            payload = asyncio.run(
                service.ask(db, domain_ids=[domain_id], question="把我们的 ERP 库接进来",
                            principal_role="publisher")
            )
    finally:
        c.AsyncOpenAI = orig  # type: ignore[assignment]

    assert payload.get("grounding_refused") is not True, payload.get("answer")
    assert payload["skill"] == "onboard"
    assert payload["onboard_proposals"] and payload["onboard_proposals"][0]["name"] == "ERP 只读库"
    assert "onboard_proposal" in [b["type"] for b in answer_to_blocks(payload)]
    assert _ds_count() == before  # 只读不变式：提案不建源


# ------------- 口径形式化：让模型出可编译的表达式（create 技能） -------------


def _expr_args(**over) -> dict:
    base = {
        "display_name": "订单总额",
        "logic_type": "metric",
        "fields": [{"alias": "amt", "object": "order", "property": "amount"}],
        "body": {"operation": "sum", "args": [{"ref": "amt"}]},
        "summary": "订单金额求和",
    }
    base.update(over)
    return base


def test_propose_expression_compiles_and_returns_real_sql():
    """提案里带的是**编译并自证过的 SQL**，不是自然语言承诺。"""
    domain_id, onto, _aliases = _seed_golden_domain()
    with SessionLocal() as db:
        r, summary, is_error = svc._dispatch_propose_expression(
            db, domain_id=domain_id, ontology_id=onto, args=_expr_args()
        )
    assert is_error is False, r
    assert "SUM" in r["compiled_sql"] and "amount" in r["compiled_sql"]
    assert r["expression_json"]["type"] == "metric"
    # refs 由服务端从本体解析，模型给的只是别名
    assert r["expression_json"]["refs"][0]["object_name"] == "order"
    assert r["expression_json"]["refs"][0]["property_name"] == "amount"
    assert r["create_payload"]["expression_json"] == r["expression_json"]
    assert any("SUM" in line for line in r["caliber_trace"])
    assert "编译通过" in summary


def test_propose_expression_rejects_unknown_field_with_candidates():
    """字段编错时**当场判错并给出该对象真实有哪些字段**——模型据此改，不必再猜一轮。"""
    domain_id, onto, _aliases = _seed_golden_domain()
    with SessionLocal() as db:
        r, _s, is_error = svc._dispatch_propose_expression(
            db, domain_id=domain_id, ontology_id=onto,
            args=_expr_args(fields=[{"alias": "amt", "object": "order", "property": "amt"}]),
        )
    assert is_error is True
    assert r["code"] == "unknown_property"
    assert "amount" in r["available_columns"]


def test_propose_expression_rejects_illegal_aggregation():
    """语义代数照样拦：对类别字段求和编不过，提案到不了用户面前。"""
    domain_id, onto, _aliases = _seed_golden_domain()
    with SessionLocal() as db:
        r, _s, is_error = svc._dispatch_propose_expression(
            db, domain_id=domain_id, ontology_id=onto,
            args=_expr_args(
                fields=[{"alias": "st", "object": "order", "property": "status"}],
                body={"operation": "sum", "args": [{"ref": "st"}]},
            ),
        )
    assert is_error is True
    assert r["code"] == "illegal_aggregation"


def test_propose_expression_rejects_undeclared_ref():
    """表达式引用了没声明的别名 → 判错。

    放过的话 _ref_of 会静默解析成 None，SUM 退化成 COUNT(*)：跑得通、算错数。
    """
    domain_id, onto, _aliases = _seed_golden_domain()
    with SessionLocal() as db:
        r, _s, is_error = svc._dispatch_propose_expression(
            db, domain_id=domain_id, ontology_id=onto,
            args=_expr_args(body={"operation": "sum", "args": [{"ref": "nope"}]}),
        )
    assert is_error is True and r["code"] == "undeclared_ref"


def test_propose_expression_tag_requires_labels():
    """标签每个分支都要有标签值，否则编出来只是一列 NULL——编译器拦下。"""
    domain_id, onto, _aliases = _seed_golden_domain()
    with SessionLocal() as db:
        ok, _s1, e1 = svc._dispatch_propose_expression(
            db, domain_id=domain_id, ontology_id=onto,
            args=_expr_args(
                display_name="大额订单", logic_type="tag",
                body={"cases": [
                    {"when": {"left": {"ref": "amt"}, "op": ">", "right": {"value": 1000}},
                     "then": {"value": "大额"}},
                    {"when": None, "then": {"value": "普通"}},
                ]},
            ),
        )
        bad, _s2, e2 = svc._dispatch_propose_expression(
            db, domain_id=domain_id, ontology_id=onto,
            args=_expr_args(
                display_name="大额订单", logic_type="tag",
                body={"cases": [
                    {"when": {"left": {"ref": "amt"}, "op": ">", "right": {"value": 1000}},
                     "then": {"value": None}},
                ]},
            ),
        )
    assert e1 is False and "CASE" in ok["compiled_sql"]
    assert e2 is True and bad["code"] == "incomplete_tag"


def test_propose_expression_patches_existing_logic():
    """给已有草稿口径补表达式 → 出 update_payload（前端 PATCH），不是新建。"""
    domain_id, onto, _aliases = _seed_golden_domain()
    with SessionLocal() as db:
        r, summary, is_error = svc._dispatch_propose_expression(
            db, domain_id=domain_id, ontology_id=onto, args=_expr_args(logic_id="bl-existing"),
        )
    assert is_error is False
    assert r["logic_id"] == "bl-existing"
    assert "create_payload" not in r
    assert r["update_payload"]["expression_json"]["type"] == "metric"
    assert "补全表达式" in summary


def test_expression_flow_stays_read_only(client):
    """端到端：select_skill(create) → propose_expression 穿真实循环；
    出 draft_proposal 块（带编译 SQL），且 **ask() 不新建任何 BusinessLogic**。"""
    from app.models import BusinessLogic

    domain_id, _onto, aliases = _seed_golden_domain()

    def _count() -> int:
        with SessionLocal() as db:
            return db.query(BusinessLogic).count()

    before = _count()
    script = [
        ToolTurn([("select_skill", {"skill": "create"})]),
        ToolTurn([("propose_expression", _expr_args())]),
        FinalTurn("已按「订单金额求和」拟好指标提案，SQL 已编译通过，点确认即可创建。"),
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
                service.ask(db, domain_ids=[domain_id], question="建个订单总额指标，按金额求和",
                            principal_role="publisher")
            )
    finally:
        c.AsyncOpenAI = orig  # type: ignore[assignment]

    assert payload.get("grounding_refused") is not True, payload.get("answer")
    assert payload["draft_proposals"], payload
    assert "SUM" in payload["draft_proposals"][0]["compiled_sql"]
    assert "draft_proposal" in [b["type"] for b in answer_to_blocks(payload)]
    assert _count() == before  # 只读不变式：提案不落库


def test_react_thinking_extraction():
    """V5.1: 验证 ReAct 思考内容的提取。"""
    # 测试带 thinking 标签的内容
    text_with_thinking = "<thinking>需要先搜索销售对象</thinking>调用 search_objects"
    thinking, clean = ChatBiService._extract_thinking(text_with_thinking)
    assert thinking == "需要先搜索销售对象"
    assert clean == "调用 search_objects"

    # 测试多行 thinking
    multiline = """<thinking>
    1. 用户问销售额
    2. 需要找销售订单对象
    3. 然后用 run_sql 取数
    </thinking>
    开始执行"""
    thinking, clean = ChatBiService._extract_thinking(multiline)
    assert "用户问销售额" in thinking
    assert "需要找销售订单对象" in thinking
    assert clean == "开始执行"

    # 测试没有 thinking 标签
    no_thinking = "直接调用工具"
    thinking, clean = ChatBiService._extract_thinking(no_thinking)
    assert thinking == ""
    assert clean == "直接调用工具"

    # 测试空字符串
    thinking, clean = ChatBiService._extract_thinking("")
    assert thinking == ""
    assert clean == ""

    # 测试 None
    thinking, clean = ChatBiService._extract_thinking(None)
    assert thinking == ""
    assert clean == ""

    # 测试大小写不敏感
    upper_case = "<THINKING>大写也可以</THINKING>内容"
    thinking, clean = ChatBiService._extract_thinking(upper_case)
    assert thinking == "大写也可以"
    assert clean == "内容"


# --------------------------------------------------------------------------- 接地泄漏回归


def test_get_object_hides_unpublished_relations():
    """get_object 只能给已发布关系——它是 Data Agent 的接地集，不是浏览视图。

    实测缺陷：dispatch 调 get_object_type 时没传 published_only（该参数默认 False），
    未发布关系照样返回，且 _compact_relation 不带 status，模型无从分辨，于是答出
    「该对象挂载 17 条已发布关系」——而那 17 条一条都没发布。
    """
    from app.models import EntityStatus, ObjectType, RelationType

    domain_id, ontology_id, _ids = _seed_golden_domain()
    with SessionLocal() as db:
        order = (
            db.query(ObjectType)
            .filter(ObjectType.ontology_id == ontology_id, ObjectType.name == "order")
            .one()
        )
        customer = (
            db.query(ObjectType)
            .filter(ObjectType.ontology_id == ontology_id, ObjectType.name == "customer")
            .one()
        )
        db.add(RelationType(
            ontology_id=ontology_id, name="order_draft_link", display_name="草稿关系",
            source_object_type_id=order.id, target_object_type_id=customer.id,
            cardinality="many_to_one", structure_type="foreign_key",
            status=EntityStatus.SUGGESTED.value,
        ))
        db.commit()
        order_id = order.id

    with SessionLocal() as db:
        result, _summary, is_error = svc._dispatch_agent_tool(
            db, domain_ids=[domain_id], ontology_ids=[ontology_id],
            name="get_object", args={"object_id": order_id},
        )
    assert is_error is False
    names = {r.get("name") for r in result["relations"]}
    assert "order_of_customer" in names       # 已发布的还在
    assert "order_draft_link" not in names    # 未发布的不得出现


# --------------------------------------------------------------------------- 表达式契约


def test_propose_expression_contract_survives_tool_compaction():
    """表达式体的形状必须由 schema 承载，且该工具不被压缩掉 description。

    运行时每轮都会跑 _compact_tools_for_prompt_retry 递归删 description；此前 body 是
    裸 {"type": "object"}，压缩后模型看不到任何形状说明，只能猜（实测连挂三轮
    unsupported_operation）。
    """
    tools = c._tools_for_skill(SKILLS["create"])
    compact = ChatBiService._compact_tools_for_prompt_retry(tools)
    by_name = {t["function"]["name"]: t for t in compact}
    body = by_name["propose_expression"]["function"]["parameters"]["properties"]["body"]
    # 结构承载契约：属性名与算子枚举都在
    assert "operation" in body["properties"] and "args" in body["properties"]
    assert "count" in body["properties"]["operation"]["enum"]
    # 该工具豁免压缩，自然语言部分也还在
    assert body.get("description")


def test_build_ast_rejects_wrong_body_shape():
    """体里一个 {"ref": …} 都没有 = 形状写错了，要当场说清怎么改。"""
    import pytest

    from app.services.expression_candidate import CandidateError, build_ast

    refs = [{"ref_id": "cg", "object_type_id": "o1", "object_name": "customer_group"}]
    # 这是真实模型产出的错误形状
    bad = {"aggregation": "count", "measure": "cg.name",
           "filter": {"field": "cg.is_group", "operator": "=", "value": 0}}
    with pytest.raises(CandidateError) as ei:
        build_ast(logic_type="metric", refs=refs, body=bad)
    assert ei.value.code == "body_shape"
    assert ei.value.detail["expected_body"]["operation"]  # 附了可照抄的模板


# --------------------------------------------------------------------------- 落点列举路由


def test_landing_listing_routes_to_ops_lane():
    """「数仓里已经落地、能直接查的表有哪些」是落点列举，不是域概览。

    此前判成 structural → overview，被域概览拿本体对象清单当「数仓里的表」答掉。
    """
    for q in ("现在数仓里已经落地、能直接查的表有哪些？", "数仓里有哪些表"):
        assert ChatBiService._classify_intent(q) == "operational"
        assert ChatBiService._auto_select_skill(q) == "ops"
    # 写意图不能被吸进只读车道
    assert ChatBiService._auto_select_skill("帮我把客户分组同步到 ODS") == "task"
    ops_tools = {t["function"]["name"] for t in c._tools_for_skill(SKILLS["ops"])}
    assert "list_datasets" in ops_tools
    assert "lint_against_standard" in ops_tools  # 合规判定要能实跑校验器


# --------------------------------------------------------------------------- 拒答保留证据


def test_refusal_keeps_executed_result():
    """F4 拒的是「那句话没凭证」，不是「本轮什么都没证实」——已执行的结果要留下。"""
    refusal = {"answer": "为避免给出不准确信息…", "grounding_refused": True}
    previous = {
        "suggested_sql": "SELECT COUNT(*) FROM customer_group",
        "data_result": {"columns": [{"key": "c"}], "rows": [{"c": 5}], "truncated": False},
    }
    out = ChatBiService._carry_verified_evidence(dict(refusal), previous)
    assert out["data_result"]["rows"] == [{"c": 5}]
    assert out["suggested_sql"]
    assert "已执行" in out["answer"]
    blocks = {b["type"] for b in answer_to_blocks(out)}
    assert {"notice", "markdown", "sql", "table"} <= blocks

    # 没真跑过就不留：suggested_sql 可能是从正文里收割的、从未执行的语句
    only_prose = ChatBiService._carry_verified_evidence(
        dict(refusal), {"suggested_sql": "SELECT 1", "data_result": None}
    )
    assert only_prose.get("data_result") is None
    assert only_prose.get("suggested_sql") is None


def test_ledger_registers_result_shape_numbers():
    """行数/列数来自结果本身，复述它们不该被判成「未经查询证实的数值」。"""
    from app.services.agent_grounding import FactLedger

    ledger = FactLedger()
    ledger.add_cells(
        [{"key": "a"}, {"key": "b"}],
        [{"a": 1, "b": 2}, {"a": 3, "b": 4}, {"a": 5, "b": 6}],
    )
    assert ledger.has_numeric("3")  # 3 行
    assert ledger.has_numeric("2")  # 2 列（同时也是单元格值）
    assert "3" in ledger.provable_numbers()


def test_render_chart_rejects_non_numeric_y():
    """y 轴是柱子的高度：拿名称列当 y 画出来的图没有意义。

    回归：分组被语义证明器拦下后，模型退化成「拉明细 + 用名称列当 y」，还在正文里
    宣称「柱状图已渲染」。列名存在不等于能作图。
    """
    data_result = {
        "columns": [{"key": "parent"}, {"key": "name"}, {"key": "cnt"}],
        "rows": [
            {"parent": "All", "name": "Commercial", "cnt": 4},
            {"parent": "All", "name": "Individual", "cnt": 2},
        ],
    }
    charts: list[dict] = []
    bad, summary, is_error = svc._dispatch_render_chart(
        {"kind": "bar", "x": "parent", "y": "name"}, data_result, charts
    )
    assert is_error is True and charts == []
    assert "cnt" in bad["numeric_columns"]
    assert "非数值列" in summary

    ok, _s, err = svc._dispatch_render_chart(
        {"kind": "bar", "x": "parent", "y": "cnt"}, data_result, charts
    )
    assert err is False and charts == [{"kind": "bar", "x": "parent", "y": "cnt"}]
