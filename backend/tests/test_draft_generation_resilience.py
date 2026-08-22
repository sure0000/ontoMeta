"""草稿预生成的连接韧性契约：瞬时错误重试成功；持续失败抛错而**不**降级为确定性命名。

这些是 P0「稳定方案」的回归护栏——命名增强遇连接抖动不能整任务作废，但也不能用
technical candidate_name 悄悄顶替真实业务命名（成功块落 checkpoint，续跑补缺失块）。
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.config import settings
from app.schemas import (
    EvidenceBundle,
    ObjectTypeEvidencePack,
    PropertyEvidencePack,
)
from app.services.draft_generator import (
    DraftEnrichmentError,
    LlmResponseFormatError,
    OntologyDraftGenerator,
)
from types import SimpleNamespace


def _bundle(num_objects: int) -> EvidenceBundle:
    object_types = []
    properties = []
    for i in range(num_objects):
        candidate = f"table_{i}_di_entity"
        object_types.append(
            ObjectTypeEvidencePack(
                candidate_name=candidate,
                display_name=f"对象{i}",
                description="d" * 50,
                source_dataset_urn=f"urn:li:dataset:table_{i}",
                evidence_refs=[f"urn:li:dataset:table_{i}"],
            )
        )
        properties.append(
            PropertyEvidencePack(
                object_candidate_name=candidate,
                field_name="f0",
                display_name="字段0",
                description="d" * 30,
                data_type="string",
                evidence_refs=[f"urn:li:dataset:table_{i}#f0"],
            )
        )
    return EvidenceBundle(object_types=object_types, properties=properties, relations=[])


def _good_content(candidate: str) -> str:
    import json

    return json.dumps(
        {
            "object_types": [
                {"candidate_name": candidate, "name": "Renamed", "display_name": "业务名"}
            ]
        }
    )


class _FlakyThenGood:
    """前 ``fail_times`` 次调用抛瞬时错，之后返回合法命名。"""

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.calls = 0

    async def create(self, *, model, messages, response_format=None):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise httpx.ReadError("simulated connection reset")
        # 从 prompt 里取第一个 candidate 名回填（单对象场景足够）。
        candidate = "table_0_di_entity"
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=_good_content(candidate)))]
        )


class _AlwaysFails:
    def __init__(self) -> None:
        self.calls = 0

    async def create(self, *, model, messages, response_format=None):
        self.calls += 1
        raise httpx.RemoteProtocolError("server disconnected")


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch):
    """退避 sleep 置空，测试不真等。"""

    async def _instant(_seconds):
        return None

    monkeypatch.setattr(asyncio, "sleep", _instant)


def _gen_with(completions) -> OntologyDraftGenerator:
    gen = OntologyDraftGenerator()
    gen.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return gen


def test_transient_error_is_retried_then_succeeds(monkeypatch):
    """单块命名调用先抛 ReadError，重试后成功——任务不因瞬时抖动失败。"""
    monkeypatch.setattr(settings, "draft_chunk_retry_attempts", 3)
    monkeypatch.setattr(settings, "llm_context_budget_chars", 10_000_000)  # 走单块路径

    flaky = _FlakyThenGood(fail_times=2)
    gen = _gen_with(flaky)
    draft = asyncio.run(gen.generate(_bundle(1)))

    assert flaky.calls == 3  # 2 次失败 + 1 次成功
    # 命名增强生效（拿到 LLM 名而非 candidate 回退），且对象零丢失。
    assert len(draft.object_types) == 1
    assert draft.object_types[0].name == "Renamed"


def test_persistent_transient_error_raises_not_degrades(monkeypatch):
    """分块路径下命名调用持续失败：抛 DraftEnrichmentError，绝不静默降级出草稿。"""
    monkeypatch.setattr(settings, "draft_chunk_retry_attempts", 2)
    monkeypatch.setattr(settings, "draft_chunk_table_batch_size", 1)
    monkeypatch.setattr(settings, "llm_context_budget_chars", 1)  # 强制分块

    always = _AlwaysFails()
    gen = _gen_with(always)

    with pytest.raises(DraftEnrichmentError) as ei:
        asyncio.run(gen.generate(_bundle(3), checkpoint=None))

    assert ei.value.phase == "对象"
    assert ei.value.failed >= 1


# ---------------------------------------------------------------------------
# 「模型这次没干好活」类失败：同样重试，重试完如实失败，绝不用技术表名顶替
# ---------------------------------------------------------------------------
class _FencedThenPlain:
    """先返回代码围栏包裹的 JSON，后续返回裸 JSON——两种都必须解析出中文命名。"""

    def __init__(self) -> None:
        self.calls = 0

    async def create(self, *, model, messages, response_format=None):
        self.calls += 1
        body = _good_content("table_0_di_entity")
        content = f"```json\n{body}\n```" if self.calls == 1 else body
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
        )


def test_code_fenced_response_needs_no_retry(monkeypatch):
    """围栏 JSON 一次就该解析成功——它是格式包装，不是失败。"""
    monkeypatch.setattr(settings, "llm_context_budget_chars", 10_000_000)

    completions = _FencedThenPlain()
    gen = _gen_with(completions)
    draft = asyncio.run(gen.generate(_bundle(1)))

    assert completions.calls == 1
    assert draft.object_types[0].display_name == "业务名"


def test_truncated_response_reports_finish_reason(monkeypatch):
    """响应被截断(finish_reason=length)时，失败原因要点出截断，便于调小分块。"""
    monkeypatch.setattr(settings, "draft_chunk_retry_attempts", 1)
    monkeypatch.setattr(settings, "llm_context_budget_chars", 10_000_000)

    class _Truncated:
        async def create(self, *, model, messages, response_format=None):
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content='{"object_types": [{"nam'),
                        finish_reason="length",
                    )
                ]
            )

    gen = _gen_with(_Truncated())
    with pytest.raises(LlmResponseFormatError) as ei:
        asyncio.run(gen.generate(_bundle(1)))
    assert "finish_reason=length" in str(ei.value)
    assert "截断" in str(ei.value)


def test_stale_checkpoint_without_naming_is_recomputed(monkeypatch):
    """历史检查点里存的是「没命名」的旧结果 → 丢弃重算，而不是拿它拼出技术名草稿。"""
    monkeypatch.setattr(settings, "draft_chunk_retry_attempts", 1)
    monkeypatch.setattr(settings, "draft_chunk_table_batch_size", 1)
    monkeypatch.setattr(settings, "llm_context_budget_chars", 1)  # 强制分块

    class _MemoryCheckpoint:
        def __init__(self) -> None:
            self.data: dict = {}
            self.loads = 0

        def load(self, key: str):
            self.loads += 1
            # 老版本降级时代留下的空命名结果。
            return {"objects": {}, "properties": {}, "roles": {}}

        def save(self, key: str, value: dict) -> None:
            self.data[key] = value

    class _GoodForAny:
        def __init__(self) -> None:
            self.calls = 0

        async def create(self, *, model, messages, response_format=None):
            import json

            self.calls += 1
            payload = json.loads(messages[-1]["content"])
            objs = [
                {
                    "candidate_name": o["candidate_name"],
                    "name": o["candidate_name"].removesuffix("_di_entity"),
                    "display_name": "业务" + o["display_name"],
                }
                for o in payload.get("object_types", [])
            ]
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=json.dumps({"object_types": objs}, ensure_ascii=False)
                        )
                    )
                ]
            )

    completions = _GoodForAny()
    gen = _gen_with(completions)
    checkpoint = _MemoryCheckpoint()
    object_types, _props = asyncio.run(
        gen.generate_object_types(_bundle(2), checkpoint=checkpoint)
    )

    assert completions.calls == 2  # 两块都因缓存不合格而重算
    assert all(ot.display_name.startswith("业务") for ot in object_types)
    assert len(checkpoint.data) == 2  # 重算结果覆盖写回
