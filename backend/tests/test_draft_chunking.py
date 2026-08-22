"""草稿生成的分块、断点续跑与「零丢失」确定性组装相关单测。

核心保证:草稿结构(对象/属性/关系)完全由证据确定性组装,LLM 只做对象命名
增强。因此无论 LLM 输出如何,对象与属性一条都不会丢。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from app.config import settings
from app.schemas import (
    EvidenceBundle,
    ObjectTypeEvidencePack,
    OntologyDraftOutput,
    PropertyEvidencePack,
    RelationEvidencePack,
)
import pytest

from app.services.draft_checkpoint import DraftCheckpointStore, chunk_key
from app.services.draft_generator import (
    LlmNotConfiguredError,
    ObjectNamingIncompleteError,
    OntologyDraftGenerator,
)
from app.services.evidence_chunker import estimate_size, split_evidence, split_relations


# ---------------------------------------------------------------------------
# 构造工具
# ---------------------------------------------------------------------------
def _build_bundle(num_objects: int, fields_per_object: int) -> EvidenceBundle:
    """构造 num_objects 个对象、每个 fields_per_object 个字段,并串上链式关系。"""
    object_types = []
    properties = []
    for i in range(num_objects):
        candidate = f"table_{i}_di_entity"
        object_types.append(
            ObjectTypeEvidencePack(
                candidate_name=candidate,
                display_name=f"业务对象{i}明细表",
                description=f"对象 {i} 的描述" * 3,
                source_dataset_urn=f"urn:li:dataset:table_{i}",
                evidence_refs=[f"urn:li:dataset:table_{i}"],
            )
        )
        for j in range(fields_per_object):
            properties.append(
                PropertyEvidencePack(
                    object_candidate_name=candidate,
                    field_name=f"field_{j}",
                    display_name=f"字段{j}",
                    description=f"字段 {j} 说明" * 2,
                    data_type="string",
                    evidence_refs=[f"urn:li:dataset:table_{i}#field_{j}"],
                )
            )
    relations = []
    for i in range(num_objects - 1):
        relations.append(
            RelationEvidencePack(
                name=f"rel_{i}",
                display_name="关联",
                source_object=f"table_{i}_di_entity",
                target_object=f"table_{i + 1}_di_entity",
                cardinality="many_to_one",
                evidence_refs=[f"urn:li:dataset:table_{i}"],
            )
        )
    return EvidenceBundle(
        object_types=object_types, properties=properties, relations=relations
    )


def _mock_generator() -> OntologyDraftGenerator:
    # 测试环境无 OPENAI_API_KEY → 实例 client=None。生成入口会直接抛
    # LlmNotConfiguredError，因此这个实例只用于直接调 _parse_*/_build_* 等纯函数。
    return OntologyDraftGenerator()


def _named_objects(payload: dict) -> list[dict]:
    """按证据回传**合格**的对象命名（英文 name + 中文 display_name）。

    业务对象中文命名是硬闸(``_assert_object_naming_complete``)，只想验证属性/关系
    行为的用例得先把对象命名喂齐，否则整批会因缺名失败。
    """
    return [
        {
            "source_ref": o["source_dataset_urn"],
            "name": "biz_" + o["candidate_name"],
            "display_name": "业务" + o["display_name"],
        }
        for o in payload.get("object_types", [])
    ]


def _all_named(bundle: EvidenceBundle) -> dict:
    """为整份证据构造合格的对象命名 overrides（直接喂 _build_* 用）。"""
    return {
        ot.candidate_name: {
            "name": ot.candidate_name.removesuffix("_di_entity"),
            "display_name": f"业务{ot.candidate_name}",
            "description": None,
        }
        for ot in bundle.object_types
    }


def _summary(draft: OntologyDraftOutput):
    return (
        sorted(ot.name for ot in draft.object_types),
        sorted((p.object_type_name, p.name) for p in draft.properties),
        sorted(
            (r.source_object_type_name, r.target_object_type_name)
            for r in draft.relation_types
        ),
    )


# ---------------------------------------------------------------------------
# 桩 LLM:只返回对象命名增强,按 source_ref 回链
# ---------------------------------------------------------------------------
def _good_object_response(payload: dict) -> str:
    objs = [
        {
            "source_ref": o["source_dataset_urn"],
            "name": "biz_" + o["candidate_name"],
            "display_name": "业务_" + o["display_name"],
        }
        for o in payload.get("object_types", [])
    ]
    return json.dumps({"objectTypes": objs}, ensure_ascii=False)


class _GoodCompletions:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def create(self, *, model, messages, response_format=None):
        payload = json.loads(messages[-1]["content"])
        self.calls.append(payload)
        content = _good_object_response(payload)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
        )


def _stub_generator() -> OntologyDraftGenerator:
    gen = OntologyDraftGenerator()
    gen.model = "stub"
    gen.client = SimpleNamespace(chat=SimpleNamespace(completions=_GoodCompletions()))
    return gen


# ---------------------------------------------------------------------------
# split_evidence(纯函数)
# ---------------------------------------------------------------------------
def test_split_preserves_all_objects_and_properties():
    bundle = _build_bundle(num_objects=8, fields_per_object=6)
    budget = estimate_size(bundle) // 3
    sub_bundles, cross = split_evidence(bundle, budget)

    assert len(sub_bundles) > 1
    got_objects = [ot.candidate_name for sub in sub_bundles for ot in sub.object_types]
    assert sorted(got_objects) == sorted(ot.candidate_name for ot in bundle.object_types)
    assert len(got_objects) == len(set(got_objects))

    obj_to_chunk = {
        ot.candidate_name: idx
        for idx, sub in enumerate(sub_bundles)
        for ot in sub.object_types
    }
    total_props = 0
    for idx, sub in enumerate(sub_bundles):
        for prop in sub.properties:
            total_props += 1
            assert obj_to_chunk[prop.object_candidate_name] == idx
    assert total_props == len(bundle.properties)


def test_split_respects_budget_when_possible():
    bundle = _build_bundle(num_objects=8, fields_per_object=6)
    budget = estimate_size(bundle) // 3
    sub_bundles, _ = split_evidence(bundle, budget)
    for sub in sub_bundles:
        if len(sub.object_types) > 1:
            assert estimate_size(sub) <= budget


def test_split_relation_classification():
    bundle = _build_bundle(num_objects=8, fields_per_object=6)
    budget = estimate_size(bundle) // 3
    sub_bundles, cross = split_evidence(bundle, budget)

    intra = sum(len(sub.relations) for sub in sub_bundles)
    assert intra + len(cross) == len(bundle.relations)
    for sub in sub_bundles:
        names = {ot.candidate_name for ot in sub.object_types}
        for rel in sub.relations:
            assert rel.source_object in names and rel.target_object in names


def test_split_single_bundle_when_budget_large():
    bundle = _build_bundle(num_objects=5, fields_per_object=4)
    sub_bundles, cross = split_evidence(bundle, budget=10_000_000)
    assert len(sub_bundles) == 1
    assert cross == []
    assert len(sub_bundles[0].relations) == len(bundle.relations)


def test_split_respects_table_batch_size():
    """按表数分批是主策略：字符预算再大，也不能超过 max_tables_per_chunk。"""
    bundle = _build_bundle(num_objects=15, fields_per_object=1)
    sub_bundles, cross = split_evidence(bundle, budget=10_000_000, max_tables_per_chunk=5)
    assert len(sub_bundles) >= 3
    assert all(len(sub.object_types) <= 5 for sub in sub_bundles)
    got_objects = [ot.candidate_name for sub in sub_bundles for ot in sub.object_types]
    assert sorted(got_objects) == sorted(ot.candidate_name for ot in bundle.object_types)


def test_split_uses_settings_default_table_batch_size(monkeypatch):
    monkeypatch.setattr(settings, "draft_chunk_table_batch_size", 4)
    bundle = _build_bundle(num_objects=10, fields_per_object=1)
    sub_bundles, _ = split_evidence(bundle, budget=10_000_000)
    assert all(len(sub.object_types) <= 4 for sub in sub_bundles)
    assert len(sub_bundles) >= 3


# ---------------------------------------------------------------------------
# split_relations(纯函数):关系流水线的独立分块，覆盖跨对象块关系
# ---------------------------------------------------------------------------
def test_split_relations_zero_loss_and_no_properties():
    bundle = _build_bundle(num_objects=8, fields_per_object=6)
    sub_bundles = split_relations(bundle, budget=10_000_000, max_relations_per_chunk=100)
    assert len(sub_bundles) == 1
    got_relations = [r.name for r in sub_bundles[0].relations]
    assert sorted(got_relations) == sorted(r.name for r in bundle.relations)
    # 关系流水线子包不携带 properties(不参与结构组装，只做业务命名)。
    assert all(sub.properties == [] for sub in sub_bundles)


def test_split_relations_respects_batch_size_and_covers_cross_chunk_relations():
    """关系分块不受对象分块边界限制:即便两端对象落在不同对象块,关系也会被
    完整分到关系流水线的某个块里,不会像旧版 cross_relations 那样被丢弃。"""
    bundle = _build_bundle(num_objects=8, fields_per_object=6)
    sub_bundles = split_relations(bundle, budget=10_000_000, max_relations_per_chunk=2)
    assert all(len(sub.relations) <= 2 for sub in sub_bundles)
    total = sum(len(sub.relations) for sub in sub_bundles)
    assert total == len(bundle.relations)  # 零丢失，覆盖所有跨块关系

    # 每个子包携带的 object_types 只是该批关系两端对象的概要(无 properties)。
    for sub in sub_bundles:
        names_in_chunk = {ot.candidate_name for ot in sub.object_types}
        for rel in sub.relations:
            assert rel.source_object in names_in_chunk
            assert rel.target_object in names_in_chunk


def test_split_relations_respects_char_budget():
    bundle = _build_bundle(num_objects=8, fields_per_object=6)
    budget = estimate_size(EvidenceBundle(relations=bundle.relations)) // 3
    sub_bundles = split_relations(bundle, budget=budget, max_relations_per_chunk=1000)
    assert len(sub_bundles) > 1


def test_split_relations_uses_settings_default_batch_size(monkeypatch):
    monkeypatch.setattr(settings, "draft_chunk_relation_batch_size", 2)
    bundle = _build_bundle(num_objects=8, fields_per_object=1)
    sub_bundles = split_relations(bundle, budget=10_000_000)
    assert all(len(sub.relations) <= 2 for sub in sub_bundles)


# ---------------------------------------------------------------------------
# 零丢失:确定性组装
# ---------------------------------------------------------------------------
def test_build_preserves_all_structure():
    bundle = _build_bundle(num_objects=5, fields_per_object=4)
    gen = _mock_generator()
    draft = gen._build_draft_from_evidence(bundle, _all_named(bundle))
    assert len(draft.object_types) == 5
    assert len(draft.properties) == 20
    assert len(draft.relation_types) == 4
    # 标识名取 LLM 给的 name。
    assert {ot.name for ot in draft.object_types} == {f"table_{i}" for i in range(5)}
    # 业务名全是中文，不是技术表名。
    assert all(ot.display_name.startswith("业务") for ot in draft.object_types)
    # 属性均有必填字段。
    assert all(p.object_type_name and p.name and p.display_name for p in draft.properties)


def test_build_without_object_naming_raises():
    """没有 LLM 命名就组装 → 抛错，绝不用技术表名顶替（反降级红线）。"""
    bundle = _build_bundle(num_objects=3, fields_per_object=2)
    gen = _mock_generator()
    with pytest.raises(ObjectNamingIncompleteError):
        gen._build_draft_from_evidence(bundle, {})


def test_build_with_english_display_name_raises():
    """LLM 把表名原样当业务名（英文）→ 视为没完成命名，抛错。"""
    bundle = _build_bundle(num_objects=2, fields_per_object=2)
    gen = _mock_generator()
    overrides = {
        ot.candidate_name: {
            "name": "table",
            "display_name": ot.candidate_name,  # 英文技术名
            "description": None,
        }
        for ot in bundle.object_types
    }
    with pytest.raises(ObjectNamingIncompleteError) as ei:
        gen._build_draft_from_evidence(bundle, overrides)
    assert "不是中文" in str(ei.value)


def test_overrides_applied_and_propagated_to_props_and_relations():
    bundle = _build_bundle(num_objects=3, fields_per_object=2)
    gen = _mock_generator()
    overrides = _all_named(bundle)
    overrides["table_0_di_entity"] = {
        "name": "payment",
        "display_name": "支付",
        "description": "d",
    }
    draft = gen._build_draft_from_evidence(bundle, overrides)
    names = {ot.name for ot in draft.object_types}
    assert "payment" in names  # override 生效
    assert "table_1" in names  # 其余对象取各自的 LLM 命名
    # table_0 的属性 object_type_name 应同步为 payment。
    assert any(
        p.object_type_name == "payment" for p in draft.properties
    )
    # 关系端点也应同步为 payment。
    assert any(r.source_object_type_name == "payment" for r in draft.relation_types)


def test_zero_loss_single_shot(monkeypatch):
    bundle = _build_bundle(num_objects=6, fields_per_object=4)
    gen = _stub_generator()
    monkeypatch.setattr(settings, "llm_context_budget_chars", 10_000_000)
    draft = asyncio.run(gen.generate(bundle))
    assert len(gen.client.chat.completions.calls) == 1
    assert len(draft.object_types) == 6
    assert len(draft.properties) == 24
    assert len(draft.relation_types) == 5
    # override 按 source_ref 命中。
    assert {ot.name for ot in draft.object_types} == {
        f"biz_table_{i}_di_entity" for i in range(6)
    }
    assert all(p.object_type_name.startswith("biz_") for p in draft.properties)


def test_bad_llm_response_fails_loudly(monkeypatch):
    """LLM 返回垃圾(无对象数组)→ 重试后失败并提示，绝不静默落回技术表名。"""

    class _BadCompletions:
        def __init__(self) -> None:
            self.count = 0

        async def create(self, *, model, messages, response_format=None):
            self.count += 1
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content='{"garbage":true}'))]
            )

    bundle = _build_bundle(num_objects=5, fields_per_object=3)
    gen = OntologyDraftGenerator()
    gen.model = "x"
    completions = _BadCompletions()
    gen.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    monkeypatch.setattr(settings, "llm_context_budget_chars", 10_000_000)
    monkeypatch.setattr(settings, "draft_chunk_retry_attempts", 2)

    with pytest.raises(ObjectNamingIncompleteError):
        asyncio.run(gen.generate(bundle))
    assert completions.count == 2  # 换采样重问过，仍不合格才失败


def test_code_fenced_json_is_parsed(monkeypatch):
    """回归：模型把 JSON 裹进 ```json 围栏（GLM 实测行为）时仍能解析出中文命名。

    这曾是「业务对象名全是英文表名」的真因——围栏让 json.loads 失败，旧实现
    回退空字典，整域几十块命名静默落空，任务却显示成功。
    """

    class _FencedCompletions:
        async def create(self, *, model, messages, response_format=None):
            payload = json.loads(messages[-1]["content"])
            body = json.dumps(
                {"objectTypes": _named_objects(payload)}, ensure_ascii=False
            )
            content = f"```json\n{body}\n```"
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
            )

    bundle = _build_bundle(num_objects=3, fields_per_object=2)
    gen = OntologyDraftGenerator()
    gen.model = "x"
    gen.client = SimpleNamespace(chat=SimpleNamespace(completions=_FencedCompletions()))
    monkeypatch.setattr(settings, "llm_context_budget_chars", 10_000_000)

    draft = asyncio.run(gen.generate(bundle))
    assert len(draft.object_types) == 3
    assert all(ot.display_name.startswith("业务") for ot in draft.object_types)


def test_prose_wrapped_json_is_parsed(monkeypatch):
    """模型在 JSON 前后加解说文字时，截取最外层对象仍能解析。"""

    class _ProseCompletions:
        async def create(self, *, model, messages, response_format=None):
            payload = json.loads(messages[-1]["content"])
            body = json.dumps(
                {"objectTypes": _named_objects(payload)}, ensure_ascii=False
            )
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content=f"好的，结果如下：\n{body}\n以上。")
                    )
                ]
            )

    bundle = _build_bundle(num_objects=2, fields_per_object=2)
    gen = OntologyDraftGenerator()
    gen.model = "x"
    gen.client = SimpleNamespace(chat=SimpleNamespace(completions=_ProseCompletions()))
    monkeypatch.setattr(settings, "llm_context_budget_chars", 10_000_000)

    draft = asyncio.run(gen.generate(bundle))
    assert all(ot.display_name.startswith("业务") for ot in draft.object_types)


def test_generate_without_llm_raises():
    """未配置 LLM → 三个生成入口都抛 LlmNotConfiguredError，而不是出技术名草稿。"""
    bundle = _build_bundle(num_objects=2, fields_per_object=2)
    gen = _mock_generator()
    assert gen.client is None
    for coro in (
        lambda: gen.generate(bundle),
        lambda: gen.generate_object_types(bundle),
        lambda: gen.generate_relations(bundle),
    ):
        with pytest.raises(LlmNotConfiguredError):
            asyncio.run(coro())


def test_zero_loss_under_top_level_array(monkeypatch):
    """回归:LLM 未遵守 json_object、返回顶层 JSON 数组时,不再抛
    ``'list' object has no attribute 'get'``,而是按调用语境归一化并保留命名。

    该数组会被 ``_coerce_llm_response`` 归到对象命名调用的 object_types,因此
    命名增强仍生效,且对象/属性/关系一条不丢。
    """

    class _TopLevelArrayCompletions:
        async def create(self, *, model, messages, response_format=None):
            payload = json.loads(messages[-1]["content"])
            # 裸数组(无 {objectTypes: ...} 包裹),模拟不守规矩的 provider。
            content = json.dumps(_named_objects(payload), ensure_ascii=False)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
            )

    bundle = _build_bundle(num_objects=4, fields_per_object=3)
    gen = OntologyDraftGenerator()
    gen.model = "x"
    gen.client = SimpleNamespace(
        chat=SimpleNamespace(completions=_TopLevelArrayCompletions())
    )
    monkeypatch.setattr(settings, "llm_context_budget_chars", 10_000_000)

    draft = asyncio.run(gen.generate(bundle))
    assert len(draft.object_types) == 4
    assert len(draft.properties) == 12
    assert len(draft.relation_types) == 3
    # 顶层数组被归一化后命名增强仍生效。
    assert {ot.name for ot in draft.object_types} == {
        f"biz_table_{i}_di_entity" for i in range(4)
    }


def test_top_level_single_wrapper_array_is_unwrapped(monkeypatch):
    """LLM 用单元素数组包裹了对象 ``[ {objectTypes: [...]} ]`` 时正确拆包。"""

    class _WrappedCompletions:
        async def create(self, *, model, messages, response_format=None):
            payload = json.loads(messages[-1]["content"])
            content = json.dumps(
                [{"objectTypes": _named_objects(payload)}], ensure_ascii=False
            )
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
            )

    bundle = _build_bundle(num_objects=3, fields_per_object=2)
    gen = OntologyDraftGenerator()
    gen.model = "x"
    gen.client = SimpleNamespace(chat=SimpleNamespace(completions=_WrappedCompletions()))
    monkeypatch.setattr(settings, "llm_context_budget_chars", 10_000_000)

    draft = asyncio.run(gen.generate(bundle))
    assert len(draft.object_types) == 3
    assert {ot.name for ot in draft.object_types} == {
        f"biz_table_{i}_di_entity" for i in range(3)
    }


def test_partial_object_naming_fails(monkeypatch):
    """LLM 只命名了部分对象（或漏了 display_name）→ 整批判不合格，失败并点名缺谁。

    宁可失败重来，也不让「一半中文名、一半技术表名」的草稿落库。
    """

    class _PartialCompletions:
        async def create(self, *, model, messages, response_format=None):
            payload = json.loads(messages[-1]["content"])
            objs = payload.get("object_types", [])
            # 只给第一个对象命名,且故意省略 display_name。
            out = []
            if objs:
                out.append({"source_ref": objs[0]["source_dataset_urn"], "name": "vip"})
            content = json.dumps({"objectTypes": out}, ensure_ascii=False)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
            )

    bundle = _build_bundle(num_objects=4, fields_per_object=3)
    gen = OntologyDraftGenerator()
    gen.model = "x"
    gen.client = SimpleNamespace(chat=SimpleNamespace(completions=_PartialCompletions()))
    monkeypatch.setattr(settings, "llm_context_budget_chars", 10_000_000)
    monkeypatch.setattr(settings, "draft_chunk_retry_attempts", 1)

    with pytest.raises(ObjectNamingIncompleteError) as ei:
        asyncio.run(gen.generate(bundle))
    assert "4 张表未拿到业务命名" in str(ei.value)


# ---------------------------------------------------------------------------
# _parse_object_overrides 回链
# ---------------------------------------------------------------------------
def test_parse_object_overrides_multi_key_matching():
    gen = _mock_generator()
    bundle = _build_bundle(num_objects=2, fields_per_object=1)
    raw = {
        "objectTypes": [
            # 按 source_ref 命中。
            {"source_ref": "urn:li:dataset:table_0", "name": "payment", "display_name": "支付"},
            # 按 refine 后同名命中(table_1)。
            {"name": "table_1", "display_name": "订单"},
            # 无法回链 → 跳过。
            {"name": "totally_unknown"},
        ]
    }
    overrides = gen._parse_object_overrides(raw, bundle)
    assert overrides["table_0_di_entity"]["name"] == "payment"
    assert overrides["table_1_di_entity"]["display_name"] == "订单"
    assert len(overrides) == 2


# ---------------------------------------------------------------------------
# 分块 ↔ 单次等价 + 零丢失
# ---------------------------------------------------------------------------
def test_chunked_equivalent_and_zero_loss(monkeypatch):
    bundle = _build_bundle(num_objects=6, fields_per_object=4)
    gen = _stub_generator()

    monkeypatch.setattr(settings, "llm_context_budget_chars", 10_000_000)
    single = asyncio.run(gen.generate(bundle))
    assert len(gen.client.chat.completions.calls) == 1

    gen.client.chat.completions.calls.clear()
    monkeypatch.setattr(settings, "llm_context_budget_chars", 50)
    chunked = asyncio.run(gen.generate(bundle))
    assert len(gen.client.chat.completions.calls) > 1

    assert _summary(single) == _summary(chunked)
    assert len(chunked.properties) == len(bundle.properties)
    assert len(chunked.relation_types) == len(bundle.relations)


def test_chunked_progress_callback(monkeypatch):
    bundle = _build_bundle(num_objects=4, fields_per_object=3)
    gen = _stub_generator()
    monkeypatch.setattr(settings, "llm_context_budget_chars", 50)

    seen: list[tuple[int, int]] = []

    async def cb(done: int, total: int) -> None:
        seen.append((done, total))

    asyncio.run(gen.generate(bundle, progress_cb=cb))
    assert seen
    assert seen[-1][0] == seen[-1][1]
    assert all(0 < d <= t for d, t in seen)


# ---------------------------------------------------------------------------
# 断点续跑
# ---------------------------------------------------------------------------
class _FakeStore:
    def __init__(self) -> None:
        self.data: dict[str, dict] = {}

    def load(self, key: str):
        return self.data.get(key)

    def save(self, key: str, value: dict) -> None:
        self.data[key] = value


def test_checkpoint_reuse_skips_llm(monkeypatch):
    bundle = _build_bundle(num_objects=6, fields_per_object=4)
    gen = _stub_generator()
    store = _FakeStore()
    monkeypatch.setattr(settings, "llm_context_budget_chars", 50)

    first = asyncio.run(gen.generate(bundle, checkpoint=store))
    assert len(gen.client.chat.completions.calls) > 1
    assert store.data

    gen.client.chat.completions.calls.clear()
    second = asyncio.run(gen.generate(bundle, checkpoint=store))
    assert len(gen.client.chat.completions.calls) == 0
    assert _summary(first) == _summary(second)


def test_checkpoint_partial_resume(monkeypatch):
    bundle = _build_bundle(num_objects=6, fields_per_object=4)
    gen = _stub_generator()
    store = _FakeStore()
    monkeypatch.setattr(settings, "llm_context_budget_chars", 50)

    asyncio.run(gen.generate(bundle, checkpoint=store))
    sub_bundles, _ = split_evidence(bundle, 50)
    stripped = EvidenceBundle(
        object_types=sub_bundles[0].object_types,
        properties=sub_bundles[0].properties,
        relations=[],
    )
    victim_key = gen._object_chunk_key(gen._build_prompt(stripped))
    del store.data[victim_key]

    gen.client.chat.completions.calls.clear()
    asyncio.run(gen.generate(bundle, checkpoint=store))
    assert len(gen.client.chat.completions.calls) == 1


def test_object_and_relation_pipelines_checkpoint_independently(monkeypatch):
    """对象流水线成功落盘后，关系流水线失败重试不应重跑已缓存的对象块。"""
    bundle = _build_bundle(num_objects=6, fields_per_object=4)
    store = _FakeStore()
    monkeypatch.setattr(settings, "llm_context_budget_chars", 50)

    fail_relations = {"on": True}

    class _FlakyRelationCompletions:
        def __init__(self) -> None:
            self.object_calls = 0
            self.relation_calls = 0

        async def create(self, *, model, messages, response_format=None):
            payload = json.loads(messages[-1]["content"])
            if payload.get("relations"):
                # 人为让出事件循环，确保(无真实网络延迟的)对象块协程先跑完，
                # 使断言可确定性地区分"首轮对象块已完成"与"关系块失败"。
                await asyncio.sleep(0.02)
                self.relation_calls += 1
                if fail_relations["on"]:
                    raise RuntimeError("relation llm boom")
                rels = [
                    {"name": r["name"], "display_name": "结算生成"}
                    for r in payload.get("relations", [])
                ]
                content = json.dumps({"relations": rels}, ensure_ascii=False)
            else:
                self.object_calls += 1
                content = _good_object_response(payload)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
            )

    gen = OntologyDraftGenerator()
    gen.model = "stub"
    completions = _FlakyRelationCompletions()
    gen.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    try:
        asyncio.run(gen.generate(bundle, checkpoint=store))
        assert False, "expected relation pipeline failure to propagate"
    except RuntimeError:
        pass

    assert completions.object_calls > 0
    # 对象块已全部落盘缓存(哪怕关系流水线整体失败)。
    object_calls_after_first_attempt = completions.object_calls

    fail_relations["on"] = False
    draft = asyncio.run(gen.generate(bundle, checkpoint=store))

    # 重试时对象流水线全部命中缓存，不发起新的对象命名调用。
    assert completions.object_calls == object_calls_after_first_attempt
    assert completions.relation_calls > 0
    assert len(draft.relation_types) == len(bundle.relations)  # 零丢失
    assert all(r.display_name == "结算生成" for r in draft.relation_types)


def test_db_checkpoint_store_roundtrip(client):
    from app.database import SessionLocal
    from app.models import DomainContext

    db = SessionLocal()
    try:
        domain = DomainContext(
            datahub_domain_id="urn:li:domain:ckpt-rt", name="Ckpt RT"
        )
        db.add(domain)
        db.commit()
        domain_id = domain.id
    finally:
        db.close()

    store = DraftCheckpointStore(domain_id)
    assert store.load("k1") is None

    store.save("k1", {"cand": {"name": "x", "display_name": "X", "description": None}})
    loaded = store.load("k1")
    assert loaded["cand"]["name"] == "x"

    store.save("k1", {"cand2": {"name": "y"}})  # 覆盖
    assert "cand" not in store.load("k1")

    store.clear()
    assert store.load("k1") is None


# ---------------------------------------------------------------------------
# 属性中文业务名增强(LLM properties 输出)
# ---------------------------------------------------------------------------
def _good_full_response(payload: dict) -> str:
    """桩 LLM:对象命名 + 属性中文名增强都按 source_ref/field_name 回链回传。"""
    objs = [
        {
            "source_ref": o["source_dataset_urn"],
            "name": "biz_" + o["candidate_name"],
            "display_name": "业务_" + o["display_name"],
        }
        for o in payload.get("object_types", [])
    ]
    urn_by_candidate = {
        o["candidate_name"]: o["source_dataset_urn"] for o in payload.get("object_types", [])
    }
    props = [
        {
            "object_source_ref": urn_by_candidate[p["object_candidate_name"]],
            "field_name": p["field_name"],
            "display_name": "中文_" + p["field_name"],
        }
        for p in payload.get("properties", [])
    ]
    return json.dumps({"objectTypes": objs, "properties": props}, ensure_ascii=False)


class _GoodFullCompletions:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def create(self, *, model, messages, response_format=None):
        payload = json.loads(messages[-1]["content"])
        self.calls.append(payload)
        content = _good_full_response(payload)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
        )


def _stub_full_generator() -> OntologyDraftGenerator:
    gen = OntologyDraftGenerator()
    gen.model = "stub"
    gen.client = SimpleNamespace(chat=SimpleNamespace(completions=_GoodFullCompletions()))
    return gen


def test_property_display_name_enriched_by_llm(monkeypatch):
    bundle = _build_bundle(num_objects=2, fields_per_object=3)
    gen = _stub_full_generator()
    monkeypatch.setattr(settings, "llm_context_budget_chars", 10_000_000)

    draft = asyncio.run(gen.generate(bundle))
    assert len(draft.properties) == 6  # 零丢失
    assert all(p.display_name.startswith("中文_") for p in draft.properties)
    # 英文标识名/数据类型不受属性中文名增强影响，仍来自证据。
    assert all(p.name.startswith("field_") for p in draft.properties)
    assert all(p.data_type == "string" for p in draft.properties)


def test_property_override_rejects_unknown_field_name(monkeypatch):
    """LLM 编造不存在的 field_name → 该条增强丢弃，属性零丢失、回退现状命名。"""
    bundle = _build_bundle(num_objects=1, fields_per_object=2)

    class _FakeFieldCompletions:
        async def create(self, *, model, messages, response_format=None):
            payload = json.loads(messages[-1]["content"])
            obj = payload["object_types"][0]
            content = json.dumps(
                {
                    "objectTypes": _named_objects(payload),
                    "properties": [
                        {
                            "object_source_ref": obj["source_dataset_urn"],
                            "field_name": "not_a_real_field",
                            "display_name": "伪造字段",
                        }
                    ],
                },
                ensure_ascii=False,
            )
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
            )

    gen = OntologyDraftGenerator()
    gen.model = "x"
    gen.client = SimpleNamespace(chat=SimpleNamespace(completions=_FakeFieldCompletions()))
    monkeypatch.setattr(settings, "llm_context_budget_chars", 10_000_000)

    draft = asyncio.run(gen.generate(bundle))
    assert len(draft.properties) == 2  # 零丢失
    assert all(p.display_name != "伪造字段" for p in draft.properties)
    assert {p.display_name for p in draft.properties} == {"字段0", "字段1"}


def test_chunked_property_overrides_merge(monkeypatch):
    bundle = _build_bundle(num_objects=6, fields_per_object=3)
    gen = _stub_full_generator()

    monkeypatch.setattr(settings, "llm_context_budget_chars", 10_000_000)
    monkeypatch.setattr(settings, "draft_chunk_table_batch_size", 10)
    single = asyncio.run(gen.generate(bundle))
    assert len(gen.client.chat.completions.calls) == 1

    gen.client.chat.completions.calls.clear()
    monkeypatch.setattr(settings, "draft_chunk_table_batch_size", 2)
    chunked = asyncio.run(gen.generate(bundle))
    assert len(gen.client.chat.completions.calls) > 1

    single_props = sorted(
        (p.object_type_name, p.name, p.display_name) for p in single.properties
    )
    chunked_props = sorted(
        (p.object_type_name, p.name, p.display_name) for p in chunked.properties
    )
    assert single_props == chunked_props
    assert len(chunked.properties) == len(bundle.properties)


# ---------------------------------------------------------------------------
# 关系业务名增强(LLM relations 输出)
# ---------------------------------------------------------------------------
def _good_relation_response(payload: dict) -> str:
    """桩 LLM:对象命名 + 关系业务名增强都按 source_ref/name 回链回传。"""
    objs = [
        {
            "source_ref": o["source_dataset_urn"],
            "name": "biz_" + o["candidate_name"],
            "display_name": "业务_" + o["display_name"],
        }
        for o in payload.get("object_types", [])
    ]
    rels = [
        {"name": r["name"], "display_name": "结算生成"}
        for r in payload.get("relations", [])
    ]
    return json.dumps({"objectTypes": objs, "relations": rels}, ensure_ascii=False)


class _GoodRelationCompletions:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def create(self, *, model, messages, response_format=None):
        payload = json.loads(messages[-1]["content"])
        self.calls.append(payload)
        content = _good_relation_response(payload)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
        )


def test_relation_display_name_enriched_by_llm(monkeypatch):
    bundle = _build_bundle(num_objects=3, fields_per_object=2)
    gen = OntologyDraftGenerator()
    gen.model = "stub"
    gen.client = SimpleNamespace(chat=SimpleNamespace(completions=_GoodRelationCompletions()))
    monkeypatch.setattr(settings, "llm_context_budget_chars", 10_000_000)

    draft = asyncio.run(gen.generate(bundle))
    assert len(draft.relation_types) == 2  # 零丢失
    # 证据里默认写死的「关联」应被 LLM 给出的业务语义词取代。
    assert all(r.display_name == "结算生成" for r in draft.relation_types)


def test_relation_override_rejects_invalid_term(monkeypatch):
    """LLM 返回超长句子式关系名(未通过 validate_relation_term)→ 丢弃，回退默认词。"""
    bundle = _build_bundle(num_objects=2, fields_per_object=1)

    class _SentenceCompletions:
        async def create(self, *, model, messages, response_format=None):
            payload = json.loads(messages[-1]["content"])
            rels = [
                {"name": r["name"], "display_name": "这是一段过长且不合法的关系描述句子。"}
                for r in payload.get("relations", [])
            ]
            content = json.dumps(
                {"objectTypes": _named_objects(payload), "relations": rels},
                ensure_ascii=False,
            )
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
            )

    gen = OntologyDraftGenerator()
    gen.model = "x"
    gen.client = SimpleNamespace(chat=SimpleNamespace(completions=_SentenceCompletions()))
    monkeypatch.setattr(settings, "llm_context_budget_chars", 10_000_000)

    draft = asyncio.run(gen.generate(bundle))
    assert len(draft.relation_types) == 1  # 零丢失
    assert draft.relation_types[0].display_name == "关联"  # 校验失败 → 回退证据默认词


def test_relation_override_rejects_unknown_name(monkeypatch):
    """LLM 编造不存在的关系 name → 该条增强丢弃，关系零丢失、回退默认词。"""
    bundle = _build_bundle(num_objects=2, fields_per_object=1)

    class _FakeRelationCompletions:
        async def create(self, *, model, messages, response_format=None):
            payload = json.loads(messages[-1]["content"])
            content = json.dumps(
                {
                    "objectTypes": _named_objects(payload),
                    "relations": [{"name": "not_a_real_relation", "display_name": "伪造"}],
                },
                ensure_ascii=False,
            )
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
            )

    gen = OntologyDraftGenerator()
    gen.model = "x"
    gen.client = SimpleNamespace(chat=SimpleNamespace(completions=_FakeRelationCompletions()))
    monkeypatch.setattr(settings, "llm_context_budget_chars", 10_000_000)

    draft = asyncio.run(gen.generate(bundle))
    assert len(draft.relation_types) == 1  # 零丢失
    assert draft.relation_types[0].display_name == "关联"


# ---------------------------------------------------------------------------
# 「仅生成业务对象」/「仅生成业务关系」独立入口：支持分开触发、并行执行
# ---------------------------------------------------------------------------
def test_generate_object_types_only_single_shot(monkeypatch):
    """仅对象入口：单次调用只请求对象命名，不产出/不依赖关系命名。"""
    bundle = _build_bundle(num_objects=3, fields_per_object=2)
    gen = _stub_full_generator()
    monkeypatch.setattr(settings, "llm_context_budget_chars", 10_000_000)

    object_types, properties = asyncio.run(gen.generate_object_types(bundle))
    assert len(gen.client.chat.completions.calls) == 1
    assert len(object_types) == 3
    assert len(properties) == 6  # 零丢失
    assert {ot.name for ot in object_types} == {
        f"biz_table_{i}_di_entity" for i in range(3)
    }
    assert all(p.display_name.startswith("中文_") for p in properties)


def test_generate_object_types_only_chunked(monkeypatch):
    """仅对象入口分块：多块并发、按内容哈希缓存，结果与单次等价、零丢失。"""
    bundle = _build_bundle(num_objects=6, fields_per_object=4)
    gen = _stub_generator()
    monkeypatch.setattr(settings, "llm_context_budget_chars", 50)

    seen: list[tuple[int, int]] = []

    async def cb(done: int, total: int) -> None:
        seen.append((done, total))

    object_types, properties = asyncio.run(
        gen.generate_object_types(bundle, progress_cb=cb)
    )
    assert len(gen.client.chat.completions.calls) > 1
    assert len(object_types) == 6
    assert len(properties) == len(bundle.properties)
    assert seen and seen[-1][0] == seen[-1][1]
    # 关系一律不出现在仅对象入口的调用 payload 里(关系交由独立入口处理)。
    assert all(not call.get("relations") for call in gen.client.chat.completions.calls)


def test_generate_relations_only_single_shot(monkeypatch):
    """仅关系入口：source/target 保留证据原始 candidate_name(未做对象命名提升)，
    供调用方按 source_dataset_urn 回链已入库对象，而不是假设这里重新命名了对象。"""
    bundle = _build_bundle(num_objects=3, fields_per_object=2)
    gen = OntologyDraftGenerator()
    gen.model = "stub"
    gen.client = SimpleNamespace(chat=SimpleNamespace(completions=_GoodRelationCompletions()))
    monkeypatch.setattr(settings, "llm_context_budget_chars", 10_000_000)

    relation_types = asyncio.run(gen.generate_relations(bundle))
    assert len(relation_types) == 2  # 零丢失
    assert all(r.display_name == "结算生成" for r in relation_types)
    # 未经 obj_name 提升，仍是证据里的原始 candidate_name。
    assert {r.source_object_type_name for r in relation_types} <= {
        ot.candidate_name for ot in bundle.object_types
    }


def test_generate_relations_only_chunked(monkeypatch):
    """仅关系入口分块：多块并发执行，零丢失，且不发起对象命名调用。"""
    bundle = _build_bundle(num_objects=8, fields_per_object=2)
    gen = OntologyDraftGenerator()
    gen.model = "stub"
    completions = _GoodRelationCompletions()
    gen.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    monkeypatch.setattr(settings, "llm_context_budget_chars", 50)
    monkeypatch.setattr(settings, "draft_chunk_relation_batch_size", 2)

    relation_types = asyncio.run(gen.generate_relations(bundle))
    assert len(completions.calls) > 1
    assert len(relation_types) == len(bundle.relations)
    assert all(r.display_name == "结算生成" for r in relation_types)
    # 每次调用 payload 都不含 properties(仅关系入口不组装对象/属性)。
    assert all(call.get("properties") == [] for call in completions.calls)


def test_generate_relations_only_checkpoint_reuse(monkeypatch):
    """仅关系入口的分块结果同样按内容哈希落检查点，重试可复用、跳过 LLM。"""
    bundle = _build_bundle(num_objects=8, fields_per_object=2)
    gen = OntologyDraftGenerator()
    gen.model = "stub"
    completions = _GoodRelationCompletions()
    gen.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    store = _FakeStore()
    monkeypatch.setattr(settings, "llm_context_budget_chars", 50)
    monkeypatch.setattr(settings, "draft_chunk_relation_batch_size", 2)

    first = asyncio.run(gen.generate_relations(bundle, checkpoint=store))
    assert len(completions.calls) > 1
    assert store.data

    completions.calls.clear()
    second = asyncio.run(gen.generate_relations(bundle, checkpoint=store))
    assert len(completions.calls) == 0
    assert sorted((r.name, r.display_name) for r in first) == sorted(
        (r.name, r.display_name) for r in second
    )


def test_object_and_relation_only_entries_compose_to_full_generate(monkeypatch):
    """仅对象 + 仅关系两个独立入口的产出，拼起来应与一体化 generate() 等价——
    验证「分开执行」重构没有改变确定性组装的语义，只是拆开了触发路径。"""
    bundle = _build_bundle(num_objects=5, fields_per_object=3)

    gen_full = _stub_full_generator()
    monkeypatch.setattr(settings, "llm_context_budget_chars", 10_000_000)
    full_draft = asyncio.run(gen_full.generate(bundle))

    gen_objects = _stub_full_generator()
    object_types, properties = asyncio.run(gen_objects.generate_object_types(bundle))

    gen_relations = OntologyDraftGenerator()
    gen_relations.model = "stub"
    gen_relations.client = SimpleNamespace(
        chat=SimpleNamespace(completions=_GoodRelationCompletions())
    )
    relation_types = asyncio.run(gen_relations.generate_relations(bundle))

    assert sorted(ot.name for ot in object_types) == sorted(
        ot.name for ot in full_draft.object_types
    )
    assert sorted((p.object_type_name, p.name) for p in properties) == sorted(
        (p.object_type_name, p.name) for p in full_draft.properties
    )
    # 关系端点这里是原始 candidate_name，full_draft 里是 obj_name 提升后的名字，
    # 二者语义上指向同一批对象，只是各自的解析口径不同(仅关系入口需调用方按
    # source_dataset_urn 回链)，故只比较关系条数与去重后的 name 集合。
    assert len(relation_types) == len(full_draft.relation_types)
    assert {r.name for r in relation_types} == {r.name for r in full_draft.relation_types}
