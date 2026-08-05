"""Data Agent golden 问题集（P0）：改造各期的**回归基线**。

**测什么、不测什么**：这里用固定 tool_call 脚本替代真实模型，所以它**不测模型智商**，
测的是「给定模型这样调工具，Agent 应该产出什么」——工具分发、结果收割、接地判定、
拒答闸门、权限降级。这些正是 DATA_AGENT_V2_PLAN 各期要动的地方，一动就该有回归信号。
真实模型的端到端表现另开 `@pytest.mark.live` 手动跑，不进 CI（不确定、要密钥、要网络）。

**五类覆盖**（沿用 PLAN §3）：概览 / 检索取详 / 取数 / 守卫（拒绝与修复信号）/ 应拒答。

新增用例只需往 `GOLDEN_CASES` 追加：`script` 是模型的行为，`expect` 是 Agent 的契约。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolTurn:
    """模型的一轮工具调用（可并发多个）。"""

    calls: list[tuple[str, dict]]


@dataclass
class FinalTurn:
    """模型的收尾作答轮。

    P4.5 起只消耗**一次** LLM 调用——服务端直接采用本轮 content。
    （此前会把 content 丢掉再发一次全上下文请求重新生成，白付一轮 prefill。）

    ``empty_content=True`` 模拟「空 content + 无 tool_calls」的少数模型，
    用于覆盖那条仍需补一次收尾轮的兜底路径。
    """

    text: str
    empty_content: bool = False
    # P4.3 自愈回环：校验不过时模型重写出的答案。None 表示「重写后仍是原样」
    # （模型没救回来），用于验证仍会拒答。
    repair_text: str | None = None


@dataclass
class GoldenCase:
    id: str
    category: str  # overview | lookup | query | guard | refuse
    question: str
    script: list[Any]
    # ---- 契约断言（None 表示不关心）----
    expect_tools: list[str] | None = None          # 期望被调用过的工具（子集匹配）
    expect_refused: bool | None = None             # 是否应触发拒答
    expect_sql_executed: bool | None = None        # run_sql 是否真的执行了
    expect_rejection_code: str | None = None       # 期望的 soundness 拒绝码
    expect_hint_keys: list[str] = field(default_factory=list)  # 拒绝提示里必须有的键
    expect_llm_calls: int | None = None
    expect_suggested_sql_contains: str | None = None   # 收割到的 SQL 必须含此片段
    expect_caliber_from_compiler: bool = False         # 口径卡须来自编译器而非事后反推
    expect_answer_contains: str | None = None
    expect_repairs: int | None = None                  # P4.3 触发了几次自愈重写
    expect_repairs_succeeded: int | None = None        # 其中几次把答案救回来了
    expect_clarification: bool = False                 # P4.1 是否以澄清反问收场
    principal_role: str = "publisher"
    note: str = ""


# ---------------------------------------------------------------------------
# 用例集。种子本体见 test_chat_bi_golden.py:_seed_golden_domain：
#   order(订单)   : amount(measure) / status(categorical) / order_date(temporal) / customer_id(identifier)
#   customer(客户): id(identifier) / region(categorical) / customer_name(textual)
#   关系 order → customer（many_to_one, foreign_key）
#   口径 order_total(订单总额)
# ---------------------------------------------------------------------------

GOLDEN_CASES: list[GoldenCase] = [
    # ---------------- 概览 ----------------
    GoldenCase(
        id="overview_objects",
        category="overview",
        question="这个数据域有哪些业务对象？",
        script=[
            ToolTurn([("get_domain_overview", {})]),
            FinalTurn("该域已发布 2 个业务对象：「订单」与「客户」。"),
        ],
        expect_tools=["get_domain_overview"],
        expect_refused=False,
        expect_llm_calls=2,  # 1 工具轮 + 1 收尾轮（P4.5 前收尾要 2 次）
    ),
    GoldenCase(
        id="overview_empty_final_content",
        category="overview",
        question="这个数据域有哪些业务对象？",
        script=[
            ToolTurn([("get_domain_overview", {})]),
            FinalTurn("该域已发布 2 个业务对象：「订单」与「客户」。", empty_content=True),
        ],
        expect_tools=["get_domain_overview"],
        expect_refused=False,
        expect_llm_calls=3,  # 空 content → 补一次显式收尾轮（兜底路径）
        note="P4.5 兜底：少数模型返回空 content + 无 tool_calls 时仍须补一轮，答案不能丢",
    ),
    # ---------------- 检索取详 ----------------
    GoldenCase(
        id="lookup_object_fields",
        category="lookup",
        question="订单对象有哪些字段？",
        script=[
            ToolTurn([("search_objects", {"keyword": "订单"})]),
            ToolTurn([("get_object", {"object_id": "@order"})]),
            FinalTurn("「订单」包含 `amount`、`status`、`order_date`、`customer_id` 四个字段。"),
        ],
        expect_tools=["search_objects", "get_object"],
        expect_refused=False,
    ),
    GoldenCase(
        id="lookup_logic_caliber",
        category="lookup",
        question="订单总额的口径是什么？",
        script=[
            ToolTurn([("search_logics", {"keyword": "订单总额"})]),
            ToolTurn([("get_logic", {"logic_id": "@order_total"})]),
            FinalTurn("「订单总额」的口径是对订单金额求和。"),
        ],
        expect_tools=["search_logics", "get_logic"],
        expect_refused=False,
    ),
    # ---------------- 取数 ----------------
    GoldenCase(
        id="query_sql_no_datasource",
        category="query",
        question="各状态的订单金额合计是多少？",
        script=[
            ToolTurn([("get_object", {"object_id": "@order"})]),
            ToolTurn([(
                "run_sql",
                {"sql": 'SELECT status, SUM(amount) FROM "order" GROUP BY status'},
            )]),
            FinalTurn("「订单」可按 `status` 分组统计 `amount`，但当前未绑定数据源，仅给出建议 SQL。"),
        ],
        expect_tools=["get_object", "run_sql"],
        expect_sql_executed=False,
        expect_refused=False,
        note="无数据源应优雅降级为「仅建议 SQL」，不报错、不拒答",
    ),
    GoldenCase(
        id="query_sql_permission_denied",
        category="query",
        question="各状态的订单金额合计是多少？",
        principal_role="editor",
        script=[
            ToolTurn([("get_object", {"object_id": "@order"})]),
            ToolTurn([(
                "run_sql",
                {"sql": 'SELECT status, SUM(amount) FROM "order" GROUP BY status'},
            )]),
            FinalTurn("「订单」可按 `status` 分组统计 `amount`；当前角色无权代跑 SQL，仅给出建议。"),
        ],
        expect_tools=["run_sql"],
        expect_sql_executed=False,
        expect_refused=False,
        note="P1.1：editor 不得让 Agent 代跑 SQL——手动 /execute 要 publisher，两条路径必须同价",
    ),
    GoldenCase(
        id="query_join_via_navigator",
        category="query",
        question="每个客户的订单金额合计是多少？",
        script=[
            ToolTurn([("find_join_path", {"from_object": "order", "to_object": "customer"})]),
            ToolTurn([(
                "run_sql",
                {"sql": 'SELECT c.id, SUM(o.amount) FROM "order" o '
                        'JOIN customer c ON o.customer_id = c.id GROUP BY c.id'},
            )]),
            FinalTurn("可沿「订单归属客户」关联后按客户汇总 `amount`；当前未绑定数据源，仅给出建议 SQL。"),
        ],
        expect_tools=["find_join_path", "run_sql"],
        expect_sql_executed=False,
        expect_refused=False,
        note="P1.2：照导航器给的 ON 写的 JOIN，必须能过语义证明（不得被 undeclared_join 拒）",
    ),
    GoldenCase(
        id="lookup_join_path_relation_grounded",
        category="lookup",
        question="订单和客户是怎么关联的？",
        script=[
            ToolTurn([("find_join_path", {"from_object": "order", "to_object": "customer"})]),
            FinalTurn("两者通过「订单归属客户」关联，基数为多对一。"),
        ],
        expect_tools=["find_join_path"],
        expect_refused=False,
        note="导航器返回的关系名必须入事实账本，否则答案一引用就被判幻觉→误拒答",
    ),
    GoldenCase(
        id="query_profile_before_literal",
        category="query",
        question="已完成的订单有多少笔？",
        script=[
            ToolTurn([("get_object", {"object_id": "@order"})]),
            ToolTurn([("profile_values", {"object_id": "order", "property": "status"})]),
            FinalTurn("需要先确认「订单」`status` 字段的真实取值口径，当前无法读取到实际取值。"),
        ],
        expect_tools=["get_object", "profile_values"],
        expect_refused=False,
        note="P1.3：写字面量前先画像。golden 域无数据源 → 画像不可用 → 如实说明，不得凭空写「已完成」",
    ),
    GoldenCase(
        id="metric_compiled_not_rewritten",
        category="metric",
        question="订单总额是多少？",
        script=[
            ToolTurn([("search_logics", {"keyword": "订单总额"})]),
            ToolTurn([("compile_metric", {"logic_id": "@order_total"})]),
            FinalTurn("「订单总额」的口径是对订单金额求和；当前未绑定数据源，仅给出编译后的 SQL。"),
        ],
        expect_tools=["search_logics", "compile_metric"],
        expect_refused=False,
        expect_suggested_sql_contains="SUM",
        expect_caliber_from_compiler=True,
        note="P3：问已有指标必须走编译器，SQL 由本体生成而非模型重写——这是口径一致性的落点",
    ),
    GoldenCase(
        id="metric_compiled_with_dimension",
        category="metric",
        question="各客户区域的订单总额？",
        script=[
            ToolTurn([("compile_metric", {
                "logic_id": "@order_total", "dimensions": ["customer.region"],
            })]),
            FinalTurn("按「客户」区域拆分「订单总额」，需沿「订单归属客户」关联后汇总。"),
        ],
        expect_tools=["compile_metric"],
        expect_refused=False,
        expect_suggested_sql_contains="JOIN",
        expect_caliber_from_compiler=True,
        note="跨对象维度：JOIN 来自语义导航器，编译产物已自证",
    ),
    # ---------------- 守卫：拒绝 + 修复信号（P1.4）----------------
    GoldenCase(
        id="guard_unknown_column_hint",
        category="guard",
        question="订单的毛利是多少？",
        script=[
            ToolTurn([("get_object", {"object_id": "@order"})]),
            ToolTurn([("run_sql", {"sql": 'SELECT SUM(gross_profit) FROM "order"'})]),
            FinalTurn("本体中「订单」没有毛利字段，无法回答。"),
        ],
        expect_tools=["run_sql"],
        expect_rejection_code="unknown_column",
        expect_hint_keys=["did_you_mean", "available_columns"],
        note="P1.4：拒绝必须带候选字段，否则模型无从自修",
    ),
    GoldenCase(
        id="guard_illegal_aggregation_hint",
        category="guard",
        question="订单状态求和是多少？",
        script=[
            ToolTurn([("get_object", {"object_id": "@order"})]),
            ToolTurn([("run_sql", {"sql": 'SELECT SUM(status) FROM "order"'})]),
            FinalTurn("对「订单」的 `status` 求和无业务意义。"),
        ],
        expect_tools=["run_sql"],
        expect_rejection_code="illegal_aggregation",
        expect_hint_keys=["measures_of_object"],
    ),
    GoldenCase(
        id="guard_unknown_table_hint",
        category="guard",
        question="供应商有多少家？",
        script=[
            ToolTurn([("run_sql", {"sql": "SELECT COUNT(*) FROM supplier"})]),
            FinalTurn("本体中没有供应商对象，无法回答。"),
        ],
        expect_tools=["run_sql"],
        expect_rejection_code="unknown_table",
        expect_hint_keys=["did_you_mean"],
    ),
    GoldenCase(
        id="card_core_object_not_hallucination",
        category="overview",
        question="这个域里关联最多的对象是哪个？",
        script=[
            ToolTurn([("get_domain_overview", {})]),
            FinalTurn("关联最多的是「订单」与「客户」两个对象。"),
        ],
        expect_tools=["get_domain_overview"],
        expect_refused=False,
        note="P2.1：语义卡上的核心对象须入事实账本，否则模型引用它们会被 F4 误判为幻觉",
    ),
    # ---------------- 应拒答 ----------------
    GoldenCase(
        id="refuse_no_tool_call",
        category="refuse",
        question="今年公司利润率怎么样？",
        script=[FinalTurn("公司今年利润率约为 18%，同比提升 2 个百分点。")],
        expect_refused=True,
        note="一个工具都没调 → 未接地 → 必须拒答，不得靠常识作答",
    ),
    GoldenCase(
        id="refuse_hallucinated_entity",
        category="refuse",
        question="订单的复购率是多少？",
        script=[
            ToolTurn([("get_object", {"object_id": "@order"})]),
            FinalTurn("「订单」的「复购率」为 32%。"),
        ],
        expect_tools=["get_object"],
        expect_refused=True,
        note="F4：「复购率」不在事实账本 → 断言不可证；重写后仍不可证 → 拒答",
    ),
    # ---------------- 澄清反问（P4.1）----------------
    GoldenCase(
        id="clarify_instead_of_guessing",
        category="clarify",
        question="按时间看一下订单总额",
        script=[
            ToolTurn([("search_logics", {"keyword": "订单总额"})]),
            ToolTurn([("ask_clarification", {
                "question": "「按时间」是指按下单日期还是支付时间？",
                "options": ["下单日期", "支付时间"],
                "reason": "订单对象有多个时间字段，按哪个汇总口径不同",
            })]),
        ],
        expect_tools=["ask_clarification"],
        expect_refused=False,
        expect_clarification=True,
        expect_answer_contains="下单日期",
        note="P4.1：只有用户能补齐的歧义应反问，不该挑一个可能错的解释硬答；"
             "且**反问不是拒答**——两者对用户的下一步指引完全不同，不能混进拒答率",
    ),
    # ---------------- 自愈回环（P4.3）----------------
    GoldenCase(
        id="repair_recovers_answer",
        category="repair",
        question="订单对象有哪些字段？",
        script=[
            ToolTurn([("get_object", {"object_id": "@order"})]),
            FinalTurn(
                # 首答多写了一个没检索过的「毛利率」→ 校验不过
                "「订单」包含 `amount`、`status` 字段，另有「毛利率」指标。",
                repair_text="「订单」包含 `amount`、`status`、`order_date`、`customer_id` 字段。",
            ),
        ],
        expect_tools=["get_object"],
        expect_refused=False,
        expect_repairs=1,
        expect_repairs_succeeded=1,
        expect_answer_contains="order_date",
        note="P4.3：删掉未接地的一句即可成立——此前这类答案被整轮拒掉，代价是整轮白跑",
    ),
    GoldenCase(
        id="repair_exhausted_still_refuses",
        category="repair",
        question="订单的毛利率是多少？",
        script=[
            ToolTurn([("get_object", {"object_id": "@order"})]),
            FinalTurn("「订单」的「毛利率」为 32%。"),  # repair_text=None → 重写后仍是原样
        ],
        expect_tools=["get_object"],
        expect_refused=True,
        expect_repairs=1,
        expect_repairs_succeeded=0,
        note="自愈只有一次机会；救不回来仍须拒答——放宽守卫比多拒一次更糟",
    ),
]
