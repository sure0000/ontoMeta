"""Data Agent golden 回归（P0）：用 LLM stub 锁住 Agent 的行为契约。

**为什么必须先有它**：DATA_AGENT_V2_PLAN 的每一期都声称能降拒绝率、省步数、减 LLM
调用。没有一个确定性的对照面，这些只能靠感觉判断，而 Agent 恰恰是最容易「改一处、
悄悄坏三处」的地方（改了工具信封 → 接地判定失灵 → 静默多拒答）。

stub 固定了模型的工具调用序列，于是每次跑的差异**只可能来自我们改的代码**。
它不测模型智商，测的是工具分发 / 结果收割 / 接地判定 / 拒答闸门 / 权限降级。

用例见 `fixtures/golden_questions.py`。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace

import pytest

from app.database import SessionLocal
from app.models import (
    BusinessLogic,
    DomainContext,
    EntityStatus,
    ObjectType,
    Ontology,
    OntologyStatus,
    Property,
    RelationType,
)
from app.services import agent_telemetry
from app.services.chat_bi import ChatBiService

from tests.fixtures.golden_questions import GOLDEN_CASES, FinalTurn, GoldenCase, ToolTurn


# --------------------------------------------------------------------------- 种子


def _seed_golden_domain() -> tuple[str, str, dict[str, str]]:
    """建 golden 用的已发布本体，返回 (domain_id, ontology_id, {别名: 真实 id})。

    用例脚本里写 `@order` 这样的别名，跑之前替换成真实 id——否则每次 uuid 变化
    都要改用例。
    """
    pub = EntityStatus.PUBLISHED.value
    uniq = uuid.uuid4().hex[:8]
    with SessionLocal() as db:
        domain = DomainContext(
            datahub_domain_id=f"urn:li:domain:golden-{uniq}", name=f"Golden域-{uniq}"
        )
        db.add(domain)
        db.flush()
        onto = Ontology(
            domain_context_id=domain.id, status=OntologyStatus.PUBLISHED.value, version=1
        )
        db.add(onto)
        db.flush()

        order = ObjectType(
            ontology_id=onto.id, name="order", display_name="订单",
            table_role="business_object", status=pub,
        )
        customer = ObjectType(
            ontology_id=onto.id, name="customer", display_name="客户",
            table_role="business_object", status=pub,
        )
        db.add_all([order, customer])
        db.flush()
        amount = Property(object_type_id=order.id, name="amount", display_name="金额",
                          semantic_type="measure", data_type="decimal", status=pub)
        db.add_all([
            amount,
            Property(object_type_id=order.id, name="status", display_name="状态",
                     semantic_type="categorical", data_type="varchar", status=pub),
            Property(object_type_id=order.id, name="order_date", display_name="下单日期",
                     semantic_type="temporal", data_type="date", status=pub),
            Property(object_type_id=order.id, name="customer_id", display_name="客户ID",
                     semantic_type="identifier", data_type="bigint", status=pub),
            Property(object_type_id=customer.id, name="id", display_name="ID",
                     semantic_type="identifier", data_type="bigint", status=pub),
            Property(object_type_id=customer.id, name="region", display_name="区域",
                     semantic_type="categorical", data_type="varchar", status=pub),
            Property(object_type_id=customer.id, name="customer_name", display_name="客户名称",
                     semantic_type="textual", data_type="varchar", status=pub),
        ])
        db.add(RelationType(
            ontology_id=onto.id, name="order_of_customer", display_name="订单归属客户",
            source_object_type_id=order.id, target_object_type_id=customer.id,
            cardinality="many_to_one", structure_type="foreign_key", status=pub,
        ))
        db.flush()  # 拿 amount.id 供口径 AST 引用
        logic = BusinessLogic(
            ontology_id=onto.id, name="order_total", display_name="订单总额",
            logic_type="metric", expression_summary="SUM(订单.金额)", status=pub,
            # 形式化口径：P3 编译器据此生成 SQL（只有摘要是编译不了的）
            expression_json=json.dumps({
                "type": "metric",
                "description": "订单金额求和",
                "refs": [{
                    "ref_id": "r1", "object_type_id": order.id, "object_name": "order",
                    "object_display_name": "订单", "property_id": amount.id,
                    "property_name": "amount", "property_display_name": "金额",
                }],
                "body": {"operation": "sum", "args": [{"ref": "r1"}], "filter": None,
                         "group_by": [], "window": None},
            }, ensure_ascii=False),
        )
        db.add(logic)
        db.commit()
        aliases = {
            "@order": order.id,
            "@customer": customer.id,
            "@order_total": logic.id,
        }
        return domain.id, onto.id, aliases


# --------------------------------------------------------------------------- LLM stub


class _StubFunction:
    def __init__(self, name: str, arguments: dict) -> None:
        self.name = name
        self.arguments = json.dumps(arguments, ensure_ascii=False)


class _StubToolCall:
    def __init__(self, idx: int, name: str, arguments: dict) -> None:
        self.id = f"call_{idx}"
        self.type = "function"
        self.function = _StubFunction(name, arguments)


class _StubStreamChunk:
    def __init__(self, text: str) -> None:
        self.choices = [SimpleNamespace(delta=SimpleNamespace(content=text))]


class _StubCompletions:
    """按脚本回放模型行为。

    非流式调用返回下一个 turn；流式调用（收尾轮）返回该 FinalTurn 的正文。
    现状实现对一个 FinalTurn 会发两次请求（详见 fixtures 里 FinalTurn 的注释），
    故流式调用**不推进**脚本游标，只重放刚消费掉的那个 FinalTurn。
    """

    def __init__(self, script: list, aliases: dict[str, str]) -> None:
        self._script = list(script)
        self._aliases = aliases
        self._cursor = 0
        self._last_final: FinalTurn | None = None
        self._final_served = False  # 收尾轮 content 是否已交付（用于区分兜底/自愈）
        self.calls = 0

    def _resolve(self, args: dict) -> dict:
        return {
            k: self._aliases.get(v, v) if isinstance(v, str) else v
            for k, v in args.items()
        }

    async def create(self, **kwargs):
        self.calls += 1
        if kwargs.get("stream"):
            # 流式调用有两种来源：①收尾轮 content 为空时的兜底；②P4.3 自愈重写。
            # 后者要能吐出**不同的**答案，否则重写必然重蹈覆辙，测不出自愈是否有效。
            if self._final_served and self._last_final is not None:
                return _StubStream(self._last_final.repair_text or self._last_final.text)
            text = self._last_final.text if self._last_final else ""
            return _StubStream(text)

        if self._cursor >= len(self._script):
            # 脚本耗尽：当作收尾轮，避免测试挂死在 agent 循环里
            self._last_final = FinalTurn("")
            return _StubResponse(content="", tool_calls=[])

        turn = self._script[self._cursor]
        self._cursor += 1
        if isinstance(turn, ToolTurn):
            calls = [
                _StubToolCall(i, name, self._resolve(args))
                for i, (name, args) in enumerate(turn.calls)
            ]
            return _StubResponse(content="", tool_calls=calls)
        assert isinstance(turn, FinalTurn)
        self._last_final = turn
        # 真实模型在收尾轮就把答案放在 content 里；P4.5 起服务端直接用它，
        # 不再另发一次请求重新生成。content 为空是少数模型的行为，
        # 由 `empty_final_content` 用例单独覆盖那条兜底路径。
        self._final_served = not turn.empty_content
        return _StubResponse(
            content="" if turn.empty_content else turn.text, tool_calls=[]
        )


class _StubStream:
    def __init__(self, text: str) -> None:
        self._text = text

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        yield _StubStreamChunk(self._text)


class _StubResponse:
    def __init__(self, content: str, tool_calls: list) -> None:
        self.choices = [
            SimpleNamespace(
                message=SimpleNamespace(content=content, tool_calls=tool_calls or None)
            )
        ]


class _StubClient:
    def __init__(self, completions: _StubCompletions) -> None:
        self.chat = SimpleNamespace(completions=completions)


# --------------------------------------------------------------------------- 运行器


async def _run_case(case: GoldenCase, domain_id: str, aliases: dict[str, str]) -> dict:
    """在 stub 下跑一个用例，返回 ask() 的 payload。"""
    import app.services.chat_bi as chat_bi_mod

    completions = _StubCompletions(case.script, aliases)
    original_client_cls = chat_bi_mod.AsyncOpenAI
    chat_bi_mod.AsyncOpenAI = lambda **_kw: _StubClient(completions)  # type: ignore[assignment]

    svc = ChatBiService()
    svc.settings_service = SimpleNamespace(  # type: ignore[assignment]
        get_llm_runtime=lambda _db: SimpleNamespace(
            api_key="stub-key", api_base_url="http://stub", model="stub-model"
        )
    )
    # golden 域刻意不绑数据源：run_sql 应降级为「仅建议 SQL」。
    # 必须显式钉死——`_resolve_domain_data_source` 没有数据域绑定，会捞到**全库任意**
    # 一个 DataSource（服务层已注明的 P1 取舍）。不钉的话，别的测试建了数据源，
    # 这里就会真去执行，基线随测试顺序漂移，就不成其为基线了。
    svc._resolve_domain_data_source = (
        lambda _db, target_catalog=None: None  # type: ignore[assignment]
    )
    # 取前后差值而不是 reset：累计基线测试要靠全局计数不断累加
    before = agent_telemetry.snapshot()
    try:
        with SessionLocal() as db:
            payload = await svc.ask(
                db,
                domain_id=domain_id,
                question=case.question,
                principal_role=case.principal_role,
            )
    finally:
        chat_bi_mod.AsyncOpenAI = original_client_cls  # type: ignore[assignment]
    payload["_llm_calls"] = completions.calls
    after = agent_telemetry.snapshot()
    payload["_telemetry"] = {
        k: after[k] - before[k] for k in ("repairs", "repairs_succeeded")
    }
    return payload


def _svc_without_data_source() -> ChatBiService:
    """golden 域不绑数据源的服务实例（理由同 `_run_case` 里的说明）。"""
    svc = ChatBiService()
    svc._resolve_domain_data_source = (
        lambda _db, target_catalog=None: None  # type: ignore[assignment]
    )
    return svc


def _steps_by_tool(payload: dict) -> dict[str, dict]:
    return {
        s["tool"]: s
        for s in (payload.get("steps") or [])
        if s.get("tool")
    }


# --------------------------------------------------------------------------- 用例


@pytest.fixture(scope="module")
def golden_domain(client):
    return _seed_golden_domain()


@pytest.mark.parametrize("case", GOLDEN_CASES, ids=lambda c: c.id)
def test_golden_case(case: GoldenCase, golden_domain):
    domain_id, _onto_id, aliases = golden_domain
    payload = asyncio.run(_run_case(case, domain_id, aliases))
    steps = _steps_by_tool(payload)

    if case.expect_tools is not None:
        missing = [t for t in case.expect_tools if t not in steps]
        assert not missing, f"{case.id}: 未调用工具 {missing}；实到 {list(steps)}"

    if case.expect_refused is not None:
        refused = bool(payload.get("grounding_refused"))
        assert refused is case.expect_refused, (
            f"{case.id}: 期望 refused={case.expect_refused}，实际 {refused}；"
            f"answer={payload.get('answer')!r}"
        )

    if case.expect_suggested_sql_contains:
        sql = payload.get("suggested_sql") or ""
        assert case.expect_suggested_sql_contains in sql, (
            f"{case.id}: 收割到的 SQL 不含 {case.expect_suggested_sql_contains!r}；sql={sql!r}"
        )

    if case.expect_caliber_from_compiler:
        cards = payload.get("caliber_decomposition") or []
        assert cards and cards[0]["label"].startswith("口径展开"), (
            f"{case.id}: 口径卡应由编译器的 caliber_trace 生成（权威），"
            f"而不是从 steps 事后反推；实际 {[c.get('label') for c in cards]}"
        )
        assert "聚合" in cards[0]["description"], cards[0]["description"]

    if case.expect_clarification:
        clar = payload.get("clarification")
        assert clar, f"{case.id}: 应以澄清反问收场；payload={payload.get('answer')!r}"
        assert clar["options"], f"{case.id}: 澄清必须给候选项"
        # 关键：反问**不是**拒答，否则前端会引导用户「换个问法」而不是回答这个问题
        assert not payload.get("grounding_refused")

    if case.expect_answer_contains:
        assert case.expect_answer_contains in (payload.get("answer") or ""), (
            f"{case.id}: 答案未包含 {case.expect_answer_contains!r}；"
            f"answer={payload.get('answer')!r}"
        )

    tel = payload["_telemetry"]
    if case.expect_repairs is not None:
        assert tel["repairs"] == case.expect_repairs, (
            f"{case.id}: 自愈次数 {tel['repairs']} != {case.expect_repairs}"
        )
    if case.expect_repairs_succeeded is not None:
        assert tel["repairs_succeeded"] == case.expect_repairs_succeeded, (
            f"{case.id}: 自愈成功数 {tel['repairs_succeeded']} != {case.expect_repairs_succeeded}"
        )

    if case.expect_llm_calls is not None:
        assert payload["_llm_calls"] == case.expect_llm_calls, (
            f"{case.id}: LLM 调用次数 {payload['_llm_calls']} != {case.expect_llm_calls}"
            "（PLAN §7.5 修完收尾轮双调用后，此期望值应下调）"
        )


@pytest.mark.parametrize(
    "case",
    [c for c in GOLDEN_CASES if c.expect_rejection_code or c.expect_sql_executed is not None],
    ids=lambda c: c.id,
)
def test_golden_run_sql_contract(case: GoldenCase, golden_domain):
    """run_sql 的三态与拒绝提示——P1.1 / P1.4 的直接锁定点。"""
    domain_id, onto_id, aliases = golden_domain

    # steps 只留摘要，拒绝码/hint 要直呼工具层拿完整结果
    sql = next(
        (args["sql"] for turn in case.script if isinstance(turn, ToolTurn)
         for name, args in turn.calls if name == "run_sql"),
        None,
    )
    assert sql, f"{case.id}: 用例未包含 run_sql 调用"

    with SessionLocal() as db:
        result, summary, is_error = _svc_without_data_source()._dispatch_agent_tool(
            db,
            domain_id=domain_id,
            ontology_id=onto_id,
            name="run_sql",
            args={"sql": sql},
            principal_role=case.principal_role,
        )

    if case.expect_sql_executed is not None:
        assert bool(result.get("executed")) is case.expect_sql_executed, (
            f"{case.id}: executed={result.get('executed')}，summary={summary}"
        )

    if case.expect_rejection_code:
        assert is_error, f"{case.id}: 期望被拒但放行了：{result}"
        assert result.get("code") == case.expect_rejection_code, (
            f"{case.id}: 拒绝码 {result.get('code')} != {case.expect_rejection_code}"
        )
        hint = result.get("hint") or {}
        missing = [k for k in case.expect_hint_keys if k not in hint]
        assert not missing, (
            f"{case.id}: 拒绝提示缺 {missing}（模型据此自修，不能只说「拒绝」）；hint={hint}"
        )


def test_permission_denied_is_degradation_not_error(golden_domain):
    """P1.1 的关键语义：权限不足是**降级**不是报错——检索仍可用，只是不代跑数。"""
    domain_id, onto_id, _ = golden_domain
    sql = 'SELECT status, SUM(amount) FROM "order" GROUP BY status'

    with SessionLocal() as db:
        svc = _svc_without_data_source()
        low, low_summary, low_err = svc._dispatch_agent_tool(
            db, domain_id=domain_id, ontology_id=onto_id, name="run_sql",
            args={"sql": sql}, principal_role="editor",
        )
        high, _, high_err = svc._dispatch_agent_tool(
            db, domain_id=domain_id, ontology_id=onto_id, name="run_sql",
            args={"sql": sql}, principal_role="publisher",
        )

    # editor：不报错、不执行、给出建议 SQL 与明确原因
    assert low_err is False, "权限不足不应作为工具错误——那会污染接地判定"
    assert low["executed"] is False
    assert low["sql"] == sql
    assert "无权" in low["reason"]
    assert "权限不足" in low_summary
    # publisher：放行到数据源解析层（本测试环境无数据源 → 降级为建议 SQL，但原因不同）
    assert high_err is False
    assert high["executed"] is False
    assert "无权" not in high.get("reason", "")


def test_telemetry_accumulates_baseline(golden_domain):
    """P0 遥测：跑完 golden set 应能吐出可对照的基线快照。"""
    domain_id, _onto_id, aliases = golden_domain
    agent_telemetry.reset()

    for case in GOLDEN_CASES:
        asyncio.run(_run_case(case, domain_id, aliases))

    snap = agent_telemetry.snapshot()
    assert snap["runs"] == len(GOLDEN_CASES)
    assert snap["avg_llm_calls"] > 0 and snap["avg_steps"] > 0
    # 应拒答类必须真的拒答，且被计入
    assert snap["refused_runs"] >= 2
    assert set(snap["refuse_kinds"]) <= {"ungrounded", "unverified"}
    # 守卫类必须留下拒绝码，run_sql 三态必须被区分
    assert "unknown_column" in snap["rejection_codes"]
    assert "suggest_only" in snap["run_sql_outcomes"]
    assert "rejected" in snap["run_sql_outcomes"]
