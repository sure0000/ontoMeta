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
from app.services.draft_checkpoint import DraftCheckpointStore, chunk_key
from app.services.draft_generator import OntologyDraftGenerator
from app.services.evidence_chunker import estimate_size, split_evidence


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
    # conftest 设置 USE_MOCK_LLM=true → 实例 use_mock=True、client=None。
    return OntologyDraftGenerator()


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
    gen.use_mock = False
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


# ---------------------------------------------------------------------------
# 零丢失:确定性组装
# ---------------------------------------------------------------------------
def test_mock_build_preserves_all_structure():
    bundle = _build_bundle(num_objects=5, fields_per_object=4)
    gen = _mock_generator()
    draft = gen._build_draft_from_evidence(bundle, {})
    assert len(draft.object_types) == 5
    assert len(draft.properties) == 20
    assert len(draft.relation_types) == 4
    # 无 override 时回退确定性命名(refine 去掉 _di_entity)。
    assert {ot.name for ot in draft.object_types} == {f"table_{i}" for i in range(5)}
    # 属性均有必填字段。
    assert all(p.object_type_name and p.name and p.display_name for p in draft.properties)


def test_overrides_applied_and_propagated_to_props_and_relations():
    bundle = _build_bundle(num_objects=3, fields_per_object=2)
    gen = _mock_generator()
    overrides = {
        "table_0_di_entity": {"name": "payment", "display_name": "支付", "description": "d"},
    }
    draft = gen._build_draft_from_evidence(bundle, overrides)
    names = {ot.name for ot in draft.object_types}
    assert "payment" in names  # override 生效
    assert "table_1" in names  # 未覆盖 → 确定性回退
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


def test_zero_loss_under_bad_llm(monkeypatch):
    """LLM 返回垃圾(无对象数组)也不能丢任何对象/属性,退回确定性命名。"""

    class _BadCompletions:
        async def create(self, *, model, messages, response_format=None):
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content='{"garbage":true}'))]
            )

    bundle = _build_bundle(num_objects=5, fields_per_object=3)
    gen = OntologyDraftGenerator()
    gen.use_mock = False
    gen.model = "x"
    gen.client = SimpleNamespace(chat=SimpleNamespace(completions=_BadCompletions()))
    monkeypatch.setattr(settings, "llm_context_budget_chars", 10_000_000)

    draft = asyncio.run(gen.generate(bundle))
    assert len(draft.object_types) == 5
    assert len(draft.properties) == 15
    assert len(draft.relation_types) == 4
    assert {ot.name for ot in draft.object_types} == {f"table_{i}" for i in range(5)}
    assert all(p.display_name for p in draft.properties)


def test_zero_loss_partial_override(monkeypatch):
    """LLM 只成功命名部分对象,其余退回确定性命名,属性一条不丢。"""

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
    gen.use_mock = False
    gen.model = "x"
    gen.client = SimpleNamespace(chat=SimpleNamespace(completions=_PartialCompletions()))
    monkeypatch.setattr(settings, "llm_context_budget_chars", 10_000_000)

    draft = asyncio.run(gen.generate(bundle))
    assert len(draft.object_types) == 4
    assert len(draft.properties) == 12  # 零丢失
    names = {ot.name for ot in draft.object_types}
    assert "vip" in names  # 部分命中
    # 省略 display_name 时回退确定性中文名(不为空)。
    vip = next(ot for ot in draft.object_types if ot.name == "vip")
    assert vip.display_name


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
    victim_key = chunk_key(gen._build_prompt(sub_bundles[0]))
    del store.data[victim_key]

    gen.client.chat.completions.calls.clear()
    asyncio.run(gen.generate(bundle, checkpoint=store))
    assert len(gen.client.chat.completions.calls) == 1


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
