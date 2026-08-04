"""端到端复现「有哪些已发布的本体有关系？」拒答问题并验证已修复。

用假的 LLM 客户端驱动完整 ask_stream：agent 先调 get_domain_overview（拿到真实已发布
关系），再产出一段提到「工具名 + 关系显示名 + 数据域名」的最终答案。断言：
  1) 全流程不拒答（grounding_refused 为假）；
  2) steps 被保留（含 get_domain_overview 步骤）。
"""

from __future__ import annotations

import asyncio
import json
import types

from app.database import SessionLocal
from app.models import (
    DomainContext,
    ObjectType,
    Ontology,
    OntologyStatus,
    RelationType,
)
from app.services.chat_bi import ChatBiService
from app.services.publish import PublishService
from app.services.settings_service import LlmRuntimeConfig


def _seed_published_with_relation(name: str) -> tuple[str, str]:
    """建两个业务对象 + 一条关系，发布后二者与关系均为 published。返回 (domain_id, ontology_id)。"""
    with SessionLocal() as db:
        domain = DomainContext(
            datahub_domain_id=f"urn:li:domain:{name}", name=name, description="no-refuse test"
        )
        db.add(domain)
        db.flush()
        ont = Ontology(domain_context_id=domain.id, status=OntologyStatus.DRAFT.value, version=0)
        db.add(ont)
        db.flush()
        order = ObjectType(
            ontology_id=ont.id, name="order", display_name="订单",
            table_role="business_object", status="edited",
        )
        customer = ObjectType(
            ontology_id=ont.id, name="customer", display_name="客户",
            table_role="business_object", status="edited",
        )
        db.add_all([order, customer])
        db.flush()
        rel = RelationType(
            ontology_id=ont.id, name="order_customer", display_name="下单客户",
            source_object_type_id=order.id, target_object_type_id=customer.id,
            status="edited",
        )
        db.add(rel)
        db.commit()
        oid = ont.id
        did = domain.id
    # 发布：两端业务对象 + 关系晋级 published
    with SessionLocal() as db:
        PublishService().publish(db, oid, operator="tester")
    return did, oid


# --------------------------- 假 LLM 客户端 ---------------------------

class _FakeToolCall:
    def __init__(self, name: str, args: dict, cid: str = "call_1"):
        self.id = cid
        self.type = "function"
        self.function = types.SimpleNamespace(name=name, arguments=json.dumps(args))


class _FakeMessage:
    def __init__(self, content: str = "", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _FakeChoice:
    def __init__(self, message=None, delta=None):
        self.message = message
        self.delta = delta


class _FakeResp:
    def __init__(self, choices):
        self.choices = choices


class _FakeStream:
    """异步可迭代：把最终答案逐字符作为 delta 吐出。"""

    def __init__(self, text: str):
        self._chunks = [
            _FakeResp([_FakeChoice(delta=types.SimpleNamespace(content=ch))]) for ch in text
        ]

    def __aiter__(self):
        self._it = iter(self._chunks)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:  # noqa: PERF203
            raise StopAsyncIteration


_FINAL_ANSWER = (
    "当前数据域已发布若干关系，例如「下单客户」（连接「订单」与「客户」）。"
    "以上通过「get_domain_overview」与「search_relations」检索得到。"
)


class _FakeCompletions:
    def __init__(self):
        self._round = 0

    async def create(self, **kwargs):
        if kwargs.get("stream"):
            return _FakeStream(_FINAL_ANSWER)
        self._round += 1
        if self._round == 1:
            # 第一轮：调用 get_domain_overview
            return _FakeResp([
                _FakeChoice(message=_FakeMessage(
                    content="", tool_calls=[_FakeToolCall("get_domain_overview", {})]
                ))
            ])
        # 第二轮：不再调工具 → 触发最终作答（流式）
        return _FakeResp([_FakeChoice(message=_FakeMessage(content="", tool_calls=None))])


class _FakeChat:
    def __init__(self):
        self.completions = _FakeCompletions()


class _FakeAsyncOpenAI:
    def __init__(self, *args, **kwargs):
        self.chat = _FakeChat()


# ---- DSML 变体：第一轮不返回原生 tool_calls，而把调用写成 DSML 文本 ----
_DSML_CALL = (
    "<｜｜DSML｜｜tool_calls>"
    '<｜｜DSML｜｜invoke name="get_domain_overview">'
    "</｜｜DSML｜｜invoke>"
    "</｜｜DSML｜｜tool_calls>"
)


class _FakeCompletionsDSML:
    def __init__(self):
        self._round = 0

    async def create(self, **kwargs):
        if kwargs.get("stream"):
            return _FakeStream(_FINAL_ANSWER)
        self._round += 1
        if self._round == 1:
            # 用 DSML 文本调用工具（无原生 tool_calls）
            return _FakeResp([_FakeChoice(message=_FakeMessage(content=_DSML_CALL, tool_calls=None))])
        return _FakeResp([_FakeChoice(message=_FakeMessage(content="", tool_calls=None))])


class _FakeAsyncOpenAIDSML:
    def __init__(self, *args, **kwargs):
        self.chat = type("_C", (), {"completions": _FakeCompletionsDSML()})()


async def _collect(svc: ChatBiService, domain_id: str, question: str) -> list[dict]:
    out: list[dict] = []
    with SessionLocal() as db:
        async for ev in svc.ask_stream(db, domain_id=domain_id, question=question):
            out.append(ev)
    return out


def test_enumeration_question_not_refused_end_to_end(client, monkeypatch):
    domain_id, _ = _seed_published_with_relation("no-refuse-domain")

    monkeypatch.setattr("app.services.chat_bi.AsyncOpenAI", _FakeAsyncOpenAI)

    svc = ChatBiService()
    # 强制走 agent 路径（有 key），并指向假客户端
    monkeypatch.setattr(
        svc.settings_service,
        "get_llm_runtime",
        lambda db: LlmRuntimeConfig(
            api_base_url="http://fake", api_key="sk-fake", model="fake-model"
        ),
    )

    events = asyncio.run(_collect(svc, domain_id, "有哪些已发布的本体有关系？"))

    done = [e for e in events if e.get("type") == "done"]
    assert len(done) == 1, [e.get("type") for e in events]
    payload = done[0]["payload"]

    # 1) 不拒答
    assert not payload.get("grounding_refused"), payload.get("answer")
    # 2) 答案确实产出（包含关系显示名）
    assert "下单客户" in payload.get("answer", "")
    # 3) 步骤保留，且含 get_domain_overview
    steps = payload.get("steps") or []
    assert any(s.get("tool") == "get_domain_overview" for s in steps), steps


def test_dsml_text_tool_calls_executed_end_to_end(client, monkeypatch):
    """模型用 DSML 文本（非原生 tool_calls）调用工具时：agent 应解析并执行，
    且答案/思考里绝不出现 DSML 标记。"""
    domain_id, _ = _seed_published_with_relation("no-refuse-dsml")
    monkeypatch.setattr("app.services.chat_bi.AsyncOpenAI", _FakeAsyncOpenAIDSML)
    svc = ChatBiService()
    monkeypatch.setattr(
        svc.settings_service,
        "get_llm_runtime",
        lambda db: LlmRuntimeConfig(
            api_base_url="http://fake", api_key="sk-fake", model="fake-model"
        ),
    )
    events = asyncio.run(_collect(svc, domain_id, "有哪些已发布的本体有关系？"))

    # 任何事件的文本里都不得出现 DSML 标记
    import json as _json

    blob = _json.dumps(events, ensure_ascii=False)
    assert "DSML" not in blob, "DSML 标记不得泄露到任何事件"

    payload = next(e["payload"] for e in events if e.get("type") == "done")
    # DSML 文本调用被成功解析执行：steps 含 get_domain_overview
    steps = payload.get("steps") or []
    assert any(s.get("tool") == "get_domain_overview" for s in steps), steps
    # 且不拒答
    assert not payload.get("grounding_refused"), payload.get("answer")
