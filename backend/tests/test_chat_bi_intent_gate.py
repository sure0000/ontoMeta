"""Data Agent 意图门控单测（结构性问题不被追加取数 SQL）。

覆盖架构强制的三层：
1. `_classify_intent` 规则分类（取数/结构才需精准接地，其余默认 general 自由作答）。
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
    # 时间窗即取数信号：问某时段的业务表现，即便没写聚合动词也需接地
    assert cls("今年公司利润率怎么样") == "analytical"


def test_classify_intent_analytical_wins_ties():
    """混合措辞里 analytical 赢平局（fail-open，绝不误伤真实取数）。"""
    cls = ChatBiService._classify_intent
    # 「字段」是结构标记、「多少」是取数标记 → 取数
    assert cls("订单有多少个字段") == "analytical"
    assert cls("各业务对象的属性数量统计") == "analytical"


def test_classify_intent_operational_is_read_only_and_specific():
    """已发生的落点/运行事实进入 operational，且不能抢走真实取数问题。"""
    cls = ChatBiService._classify_intent
    assert cls("采购订单落到哪张物理表了") == "operational"
    assert cls("这个任务为什么失败") == "operational"
    assert cls("整条链做到哪了") == "operational"
    assert cls("当前六环进度到哪一环") == "operational"
    assert cls("本体发布版本和上一版有什么差异") == "operational"
    assert cls("当前生效规约是哪版") == "operational"
    assert cls("上次草稿生成进度怎么样") == "operational"
    assert cls("重新生成的合并报告是什么") == "operational"
    assert cls("当前有哪些待复核冲突") == "operational"
    assert cls("ERP 数据源上次测通了吗") == "operational"
    assert cls("经营看板版本发布了几版") == "operational"
    assert cls("Airflow 组件部署状态是什么") == "operational"
    assert cls("生产割接到哪一步了") == "operational"
    assert cls("近30天任务失败次数是多少") == "analytical"


def test_ops_skill_exposes_only_read_record_tools():
    from app.services.chat_bi_skills import SKILLS

    tools = {t["function"]["name"] for t in c._tools_for_skill(SKILLS["ops"])}
    assert {"search_objects", "get_object", "get_landing", "get_ops_record"} <= tools
    assert not {"propose_action", "propose_pipeline", "request_form"} & tools


def test_operational_autoroute_does_not_steal_analytical_queries():
    assert ChatBiService._auto_select_skill("采购订单落到哪张物理表了") == "ops"
    assert ChatBiService._auto_select_skill("物理表有多少行") == "query"
    assert ChatBiService._auto_select_skill("创建同步任务") == "task"


def test_materialize_target_question_routes_to_read_lane_not_task():
    """V6 §1 的触发案例：「物化到哪个数据源」问的是已发生的落点，不是要建任务。

    钉住的是那条会「流畅地答错」的路径——"物化" 是 task 车道第一优先标记，不在
    operational 里认下「物化到哪」这个复合词，问题就会进建任务车道，再拿契约里的
    materialization 配置编一句「物化在默认 Doris」，与实际落点无关。
    """
    route = ChatBiService._auto_select_skill
    assert route("这个本体物化到哪个数据源？") == "ops"
    assert route("客户对象同步到哪张表了") == "ops"
    # 对照：明确的写意图仍归 task，只读车道不能把建任务吸走。
    assert route("帮我建个物化任务") == "task"
    assert route("给订单对象配置物化") == "task"


def test_classify_intent_defaults_general():
    """未命中取数/结构标记 → general（默认自由作答，不要求接地）。"""
    cls = ChatBiService._classify_intent
    assert cls("怎么创建数据任务") == "general"   # 产品 how-to，不该被拒答
    assert cls("你可以做什么") == "general"
    assert cls("你能做什么？") == "general"
    assert cls("你有什么功能") == "general"
    assert cls("你好") == "general"
    assert cls("帮我看看这个") == "general"
    assert cls("") == "general"


def test_classify_intent_precise_intents_win_over_general():
    """需要精准回答的意图优先于 general：带取数/结构标记的真问题仍要求接地。"""
    cls = ChatBiService._classify_intent
    assert cls("近30天订单总额是多少") == "analytical"
    assert cls("你能帮我统计订单数量吗") == "analytical"   # 「统计/数量」→取数
    assert cls("订单有哪些字段") == "structural"


def test_tools_narrow_removes_sql_tools_when_not_allowed():
    """sql_allowed=False：取数工具（run_sql/compile_metric）被移出工具集。"""
    full = {t["function"]["name"] for t in c._tools_for_skill(None, sql_allowed=True)}
    assert "run_sql" in full  # V5：run_sql 在 DEFAULT_TOOL_ALLOWLIST 里
    # compile_metric 不在默认集，只在 query skill 解锁

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
    """V5 工具收窄后：默认集从 _BASE_TOOL_SCHEMAS 收窄到 DEFAULT_TOOL_ALLOWLIST（9个）。"""
    from app.services.chat_bi_tool_schemas import DEFAULT_TOOL_ALLOWLIST

    default = {t["function"]["name"] for t in c._tools_for_skill(None)}
    # V5: 默认集是白名单，不再等于全部 BASE
    assert default == DEFAULT_TOOL_ALLOWLIST
    # 仍包含核心检索 + 简单查询 + select_skill
    assert {"search_objects", "get_object", "run_sql", "select_skill"} <= default


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
    service._verify_answer = lambda answer, ledger, question, **_kw: (True, [])  # type: ignore[assignment]
    return service


def _ask(service: ChatBiService, domain_id: str, question: str) -> dict:
    orig = c.AsyncOpenAI
    c.AsyncOpenAI = lambda **_k: _StubClient(service._completions)  # type: ignore[attr-defined,assignment]
    try:
        with SessionLocal() as db:
            return asyncio.run(
                service.ask(db, domain_ids=[domain_id], question=question, principal_role="publisher")
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

    def fake_dispatch(db, *, domain_ids, ontology_ids, name, args, principal_role=None, conversation_id=None):
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

    def fake_dispatch(db, *, domain_ids, ontology_ids, name, args, principal_role=None, conversation_id=None):
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

    def fake_dispatch(db, *, domain_ids, ontology_ids, name, args, principal_role=None, conversation_id=None):
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

    def fake_dispatch(db, *, domain_ids, ontology_ids, name, args, principal_role=None, conversation_id=None):
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


def test_general_question_not_refused_without_tool_hit(client):
    """一般/元问题（产品 how-to）：模型不调任何工具直接作答，也不被接地判定拦成拒答。"""
    domain_id, _onto, aliases = _seed_golden_domain()

    def fake_dispatch(db, *, domain_ids, ontology_ids, name, args, principal_role=None, conversation_id=None):
        return ({"error": f"unexpected {name}"}, "", True)  # 一般问题不该调任何工具

    script = [FinalTurn("在「数据任务」页点击新建，选择来源与目标即可创建同步/转换任务。")]
    service = _make_service(_StubCompletions(script, aliases))
    service._dispatch_agent_tool = fake_dispatch  # type: ignore[assignment]

    payload = _ask(service, domain_id, "怎么创建数据任务")

    assert not payload.get("grounding_refused"), "产品 how-to 一般问题不应被判为未接地拒答"
    assert "无法基于" not in payload.get("answer", "")
    assert "数据任务" in payload.get("answer", "")


# --------------------------------------------------------------------------- 收口 general 豁免


def test_names_published_entity_detection():
    """强判据单元测：问句含已发布实体的完整显示名（≥2 字）才算「点名本域业务」。"""
    fn = ChatBiService._question_names_published_entity
    objs = [SimpleNamespace(display_name="客户", name="customer")]
    logics = [SimpleNamespace(display_name="订单总额", name="order_total")]
    # 完整显示名入问句 → True
    assert fn("客户为什么会流失", objs, logics) is True
    assert fn("订单总额最近怎么样", objs, logics) is True
    # 英文标识入问句 → True
    assert fn("customer 表能查吗", objs, logics) is True
    # 只是碰巧共享单字（客≠客户）、或未点名任何实体 → False（不误触发接地）
    assert fn("客服电话是多少", objs, logics) is False
    assert fn("你能做什么", objs, logics) is False
    assert fn("怎么创建数据任务", objs, logics) is False
    # 无候选 → False
    assert fn("客户为什么会流失", [], []) is False


def test_general_but_names_entity_requires_grounding(client):
    """收口缺口：general 但**点名了已发布实体**的问题（「客户为什么会流失」），
    模型不查本体、纯散文常识作答 → 应被接地判定拦成拒答，不再吃 general 豁免。"""
    domain_id, _onto, aliases = _seed_golden_domain()

    def fake_dispatch(db, *, domain_ids, ontology_ids, name, args, principal_role=None, conversation_id=None):
        return ({"error": f"unexpected {name}"}, "", True)  # 模型没调任何工具

    # 纯散文常识作答：不含任何标记具名实体/带单位数值，F4 拦不住——正是要靠接地闸拦的一类
    script = [FinalTurn("客户通常会因为服务质量下降或价格因素而流失，建议加强关怀。")]
    service = _make_service(_StubCompletions(script, aliases))
    service._dispatch_agent_tool = fake_dispatch  # type: ignore[assignment]

    payload = _ask(service, domain_id, "客户为什么会流失")

    assert payload.get("grounding_refused"), "点名本域实体却零接地的常识作答应被拒答"


def test_general_names_entity_but_tool_hit_not_refused(client):
    """对照：同样点名实体，但模型**真去查了本体**（命中 get_object）→ 已接地，不拒答。

    证明收口收的是「没查就凭常识答」，不是「凡点名实体一律拦」。"""
    domain_id, _onto, aliases = _seed_golden_domain()
    order_id = aliases["@order"]

    def fake_dispatch(db, *, domain_ids, ontology_ids, name, args, principal_role=None, conversation_id=None):
        if name == "get_object":
            return (_obj_detail(order_id), "对象「订单」", False)
        return ({"error": f"unexpected {name}"}, "", True)

    script = [
        ToolTurn([("get_object", {"object_id": "@order"})]),
        FinalTurn("「订单」对象包含金额、状态等字段。"),
    ]
    service = _make_service(_StubCompletions(script, aliases))
    service._dispatch_agent_tool = fake_dispatch  # type: ignore[assignment]

    payload = _ask(service, domain_id, "订单是干什么的")

    assert not payload.get("grounding_refused"), "点名实体且已查本体不应被拒答"
