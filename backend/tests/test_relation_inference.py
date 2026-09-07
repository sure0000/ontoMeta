"""智能关系补充（P2）：LLM 判定 + 候选落库 + 人工表态。

钉住的行为：

1. 送模型的是**族**不是**对**——12 个族 vs 15244 对，这是整个设计的成立前提；
2. 判定不合格一律抛错，**不降级**：漏判、verdict 非法、判成键却没中文名；
3. 基数与方向由 distinct/rows 算，展开成对时才算，模型不参与；
4. 人工表态优先于机器：重跑推断不覆盖 confirmed/rejected；
5. `not_a_key` 的族不给确认（没有可确认的关系）。
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from app.database import SessionLocal
from app.models import DomainContext, RelationCandidateFamily
from app.services import key_family_verdict, relation_inference
from app.services.key_family import KeyColumn, KeyFamily
from app.services.key_family_verdict import (
    VerdictIncompleteError,
    family_payload,
)


def _column(table, column, distinct, rows):
    return KeyColumn(
        table=table,
        column=column,
        data_type="VARCHAR(255)",
        distinct=distinct,
        rows=rows,
        samples=("RY00000183", "RY00000330"),
    )


def _family(family_id="f_a2n8", members=None):
    return KeyFamily(
        id=family_id,
        value_shape="A<2>N<8>",
        members=tuple(
            members
            or [
                _column("ry_zl", "ry_bh", 1000, 1000),
                _column("aj_bl_zl", "bl_bh", 1205, 1571),
                _column("aj_cyry_ql", "zbr_bh", 853, 1153),
            ]
        ),
    )


def _verdict_payload(family_id="f_a2n8", **overrides):
    item = {
        "family_id": family_id,
        "verdict": "entity_key",
        "entity_name": "人员",
        "key_name": "人员编号",
        "predicate": "涉及",
        "confidence": 0.88,
        "reason": "值为 RY 前缀加 8 位序号，列名均以 bh(编号)结尾",
    }
    item.update(overrides)
    return {"families": [item]}


# --------------------------------------------------------------------------- 提示词入参


def test_prompt_carries_families_not_pairs():
    """送模型的单位是族。族里 90 列展开是 4005 对——那个量级不可能一次问完。"""
    payload = family_payload(_family())

    assert payload["family_id"] == "f_a2n8"
    assert payload["value_shape"] == "A<2>N<8>"
    assert payload["table_count"] == 3
    assert {m["table"] for m in payload["members"]} == {
        "ry_zl",
        "aj_bl_zl",
        "aj_cyry_ql",
    }
    # 成员带 distinct/rows：模型据此看得出「有没有主表」，但基数不归它算。
    assert all("distinct" in m and "rows" in m for m in payload["members"])


def test_prompt_dedupes_columns_and_caps_members():
    """90 列的族只送有限个成员，且按列名种类去重——覆盖面优先于条数。"""
    members = [_column(f"t{i}", "bl_bh", 100, 200) for i in range(30)]
    members += [_column(f"u{i}", f"col_{i}", 100, 200) for i in range(30)]
    payload = family_payload(_family(members=members))

    columns = [m["column"] for m in payload["members"]]
    assert len(columns) == len(set(columns))
    assert len(columns) <= key_family_verdict.MAX_MEMBERS_IN_PROMPT
    assert payload["column_count"] == 60  # 统计仍是全量


# --------------------------------------------------------------------------- 判定解析


def test_parse_accepts_a_well_formed_verdict():
    verdicts = key_family_verdict._parse(_verdict_payload(), [_family()])

    assert len(verdicts) == 1
    assert verdicts[0].verdict == "entity_key"
    assert verdicts[0].entity_name == "人员"
    assert verdicts[0].is_key


def test_parse_rejects_missing_family():
    """漏判即失败——不能让没判到的族悄悄消失。"""
    with pytest.raises(VerdictIncompleteError, match="漏判"):
        key_family_verdict._parse({"families": []}, [_family()])


def test_parse_rejects_invalid_verdict():
    with pytest.raises(VerdictIncompleteError, match="verdict 不合法"):
        key_family_verdict._parse(
            _verdict_payload(verdict="maybe"), [_family()]
        )


def test_parse_rejects_key_without_chinese_name():
    """判成键却给不出中文名 = 没完成命名任务。绝不用 family_id 顶替。"""
    with pytest.raises(VerdictIncompleteError, match="缺中文命名"):
        key_family_verdict._parse(
            _verdict_payload(entity_name="Person", key_name="person_no"), [_family()]
        )


def test_parse_allows_empty_names_for_not_a_key():
    verdicts = key_family_verdict._parse(
        _verdict_payload(
            verdict="not_a_key", entity_name="", key_name="", predicate=""
        ),
        [_family()],
    )
    assert not verdicts[0].is_key


def test_parse_clamps_confidence():
    verdicts = key_family_verdict._parse(_verdict_payload(confidence=7), [_family()])
    assert verdicts[0].confidence == 1.0


def test_wide_family_confidence_is_lowered_with_a_review_note():
    verdict = key_family_verdict._parse(_verdict_payload(), [_family()])[0]

    confidence, reason = relation_inference._adjust_wide_family_verdict(
        _family(), verdict, total_tables=4
    )

    assert confidence == pytest.approx(0.704)
    assert "覆盖范围过大" in reason


def test_narrow_family_confidence_is_unchanged():
    verdict = key_family_verdict._parse(_verdict_payload(), [_family()])[0]

    confidence, reason = relation_inference._adjust_wide_family_verdict(
        _family(), verdict, total_tables=6
    )

    assert confidence == verdict.confidence
    assert reason == verdict.reason


# --------------------------------------------------------------------------- 展开成对


def _row_with_members(members):
    row = RelationCandidateFamily(
        domain_context_id="d",
        run_id="r",
        family_id="f",
        value_shape="A<2>N<8>",
        verdict="entity_key",
        members_json=json.dumps(
            [
                {
                    "table": m.table,
                    "column": m.column,
                    "distinct": m.distinct,
                    "rows": m.rows,
                }
                for m in members
            ]
        ),
    )
    return row


def test_expand_pairs_computes_cardinality_and_direction():
    """有主表端 → many_to_one 且方向指向主表；两端都不唯一 → 多对多按桥表记。"""
    pairs = relation_inference.expand_pairs(
        _row_with_members(
            [
                _column("ry_zl", "ry_bh", 1000, 1000),  # 主表：近唯一
                _column("aj_bl_zl", "bl_bh", 1205, 1571),
            ]
        )
    )
    assert len(pairs) == 1
    assert pairs[0]["source_table"] == "aj_bl_zl"
    assert pairs[0]["target_table"] == "ry_zl"
    assert pairs[0]["cardinality"] == "many_to_one"
    assert pairs[0]["structure_type"] == "foreign_key"

    loose = relation_inference.expand_pairs(
        _row_with_members(
            [
                _column("aj_bl_zl", "bl_bh", 1205, 1571),
                _column("aj_cyry_ql", "zbr_bh", 853, 1153),
            ]
        )
    )
    assert loose[0]["cardinality"] == "many_to_many"
    assert loose[0]["structure_type"] == "bridge_table"


def test_expand_pairs_skips_same_table_columns():
    """同表两列是自关联，不是表间关系。"""
    pairs = relation_inference.expand_pairs(
        _row_with_members(
            [
                _column("aj_cyry_ql", "zbr_bh", 853, 1153),
                _column("aj_cyry_ql", "xbr_bh", 900, 1153),
            ]
        )
    )
    assert pairs == []


# --------------------------------------------------------------------------- 落库与表态


@pytest.fixture
def domain_id():
    db = SessionLocal()
    domain = DomainContext(
        id=str(uuid.uuid4()),
        name=f"p2-{uuid.uuid4().hex[:6]}",
        datahub_domain_id="urn:li:domain:p2",
    )
    db.add(domain)
    db.commit()
    yield domain.id
    db.query(RelationCandidateFamily).filter(
        RelationCandidateFamily.domain_context_id == domain.id
    ).delete()
    db.query(DomainContext).filter(DomainContext.id == domain.id).delete()
    db.commit()
    db.close()


def _seed(db, domain_id, *, verdict="entity_key", state="proposed", family_id="f_1"):
    row = RelationCandidateFamily(
        domain_context_id=domain_id,
        run_id="run-1",
        family_id=family_id,
        value_shape="A<2>N<8>",
        sample_values_json=json.dumps(["RY00000183"]),
        members_json=json.dumps(
            [
                {"table": "ry_zl", "column": "ry_bh", "distinct": 1000, "rows": 1000},
                {"table": "aj_bl_zl", "column": "bl_bh", "distinct": 1205, "rows": 1571},
            ]
        ),
        table_count=2,
        column_count=2,
        verdict=verdict,
        entity_name="人员",
        key_name="人员编号",
        confidence=0.9,
        state=state,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_decide_records_the_human_verdict(domain_id):
    db = SessionLocal()
    try:
        row = _seed(db, domain_id)
        decided = relation_inference.decide(
            db, row.id, state="confirmed", operator="张三"
        )
        assert decided.state == "confirmed"
        assert decided.decided_by == "张三"
        assert decided.decided_at is not None
    finally:
        db.close()


def test_not_a_key_family_cannot_be_confirmed(domain_id):
    db = SessionLocal()
    try:
        row = _seed(db, domain_id, verdict="not_a_key")
        with pytest.raises(ValueError, match="not_a_key"):
            relation_inference.decide(db, row.id, state="confirmed")
    finally:
        db.close()


def test_decide_rejects_unknown_state(domain_id):
    db = SessionLocal()
    try:
        row = _seed(db, domain_id)
        with pytest.raises(ValueError, match="confirmed 或 rejected"):
            relation_inference.decide(db, row.id, state="applied")
    finally:
        db.close()


def test_reinference_keeps_human_decision_but_refreshes_machine_part(
    domain_id, monkeypatch
):
    """人工表态优先于机器：重跑推断刷新判定与成员，但不把 confirmed 打回 proposed。"""
    from app.schemas import DataHubDomainBundle, DomainInput

    db = SessionLocal()
    try:
        decided = _seed(db, domain_id, family_id="f_a2n8", state="confirmed")
        decided.confidence = 0.4
        decided.decided_by = "李四"
        db.commit()
        decided_id = decided.id
    finally:
        db.close()

    async def fake_fetch(db, domain, *, refresh=False):
        return (
            DataHubDomainBundle(domain=DomainInput(id="x", name="x"), datasets=[]),
            True,
        )

    monkeypatch.setattr(relation_inference.datahub_bundle_cache, "fetch", fake_fetch)
    monkeypatch.setattr(
        relation_inference.key_family,
        "build_key_families",
        lambda datasets: ([_family()], relation_inference.key_family.DropStats()),
    )

    async def fake_judge(families, runtime_config=None):
        return key_family_verdict._parse(
            _verdict_payload(confidence=0.97, reason="重跑后的新判据"), families
        )

    monkeypatch.setattr(relation_inference.key_family_verdict, "judge_families", fake_judge)
    monkeypatch.setattr(
        relation_inference.SettingsService, "get_llm_runtime", lambda self, db: None
    )

    db = SessionLocal()
    try:
        import anyio

        result = anyio.run(relation_inference.infer, db, domain_id)
        assert len(result.families) == 1

        row = db.get(RelationCandidateFamily, decided_id)
        # 人的结论没被覆盖
        assert row.state == "confirmed"
        assert row.decided_by == "李四"
        # 机器那部分刷新了
        assert row.confidence == pytest.approx(0.97)
        assert row.reason == "重跑后的新判据"
        assert row.table_count == 3
    finally:
        db.close()


# --------------------------------------------------------------------------- 落地（方案落点 c）


def test_confirmed_candidate_becomes_ontology_evidence(domain_id):
    """P3 的落地就是「确认」本身：确认后，下次组装证据就带上这批关系。

    落点 (c) = 本地候选接进 evidence_builder，复用 P0 为代码包关联键铺的那条路，
    所以不需要单独的 apply 动作；``applied`` 留给写回 DataHub（P4）。
    """
    from app.services import observed_joins

    db = SessionLocal()
    try:
        row = _seed(db, domain_id)
        assert relation_inference.confirmed_joins(db, domain_id) == []

        relation_inference.decide(db, row.id, state="confirmed", operator="王五")
        joins = relation_inference.confirmed_joins(db, domain_id)

        assert len(joins) == 1
        join = joins[0]
        # 方向按区分度定：aj_bl_zl(1205/1571) 引用 ry_zl(1000/1000)
        assert (join.left_table, join.right_table) == ("aj_bl_zl", "ry_zl")
        assert join.origin == observed_joins.ORIGIN_CONFIRMED_INFERENCE
        assert "人员编号" in join.source_file
    finally:
        db.close()


def test_confirmed_inference_is_labelled_apart_from_observed_join(domain_id):
    """两种来源的描述必须分得开——不然复核的人分不清「机器见过」和「机器猜的、我确认了」。"""
    from app.schemas import DataHubDomainBundle, DatasetInput, DomainInput, FieldInput
    from app.services.evidence_builder import EvidenceBuilder

    db = SessionLocal()
    try:
        row = _seed(db, domain_id)
        relation_inference.decide(db, row.id, state="confirmed")
        joins = relation_inference.confirmed_joins(db, domain_id)
    finally:
        db.close()

    def _ds(name, column, distinct, rows):
        return DatasetInput(
            urn=f"urn:li:dataset:(urn:li:dataPlatform:mysql,{name},PROD)",
            name=name,
            row_count=rows,
            fields=[
                FieldInput(
                    name=column,
                    data_type="VARCHAR(64)",
                    unique_count=distinct,
                    sample_values=["RY00000183"],
                ),
                FieldInput(
                    name="bz", data_type="VARCHAR(64)", sample_values=["备注"]
                ),
            ],
        )

    bundle = DataHubDomainBundle(
        domain=DomainInput(id="d", name="d"),
        datasets=[
            _ds("ry_zl", "ry_bh", 1000, 1000),
            _ds("aj_bl_zl", "bl_bh", 1205, 1571),
        ],
    )
    evidence = EvidenceBuilder().build(bundle, observed_joins=joins)
    merged: dict[str, list] = {}
    EvidenceBuilder._merge_observed_joins(bundle, joins, merged)
    edge = next(iter(v for v in merged.values() if v))[0]

    assert edge.origin == "confirmed_inference"
    assert edge.confidence == 0.75
    assert "人员编号" in (edge.label or "")
    # 描述里写的是「已人工确认」，不是「代码包里真实执行过的 JOIN」
    descriptions = " ".join(r.description or "" for r in evidence.relations)
    if descriptions:
        assert "代码包里真实执行过的 JOIN" not in descriptions


def test_confirming_a_candidate_invalidates_the_evidence_cache(domain_id):
    """确认了新关系，evidence 缓存必须失效——否则下次生成草稿还是命中旧证据。"""
    from app.services import observed_joins

    db = SessionLocal()
    try:
        row = _seed(db, domain_id)
        before = observed_joins.digest(db, domain_id)
        relation_inference.decide(db, row.id, state="confirmed")
        after = observed_joins.digest(db, domain_id)
        assert before != after
    finally:
        db.close()


def test_apply_confirmed_candidate_marks_it_applied(domain_id, monkeypatch):
    from types import SimpleNamespace

    from app.services.lineage_inventory import DomainInventory, InventoryTable

    row = None
    with SessionLocal() as db:
        row = _seed(db, domain_id, state="confirmed")

    tables = [
        InventoryTable(
            urn=f"urn:li:dataset:({name})",
            name=name,
            platform="mysql",
            upstream=0,
            downstream=0,
        )
        for name in ("ry_zl", "aj_bl_zl")
    ]
    inventory = DomainInventory(
        domain_id=domain_id,
        datahub_domain_id="urn:li:domain:p2",
        tables=tuple(tables),
        name_index={table.name: table.urn for table in tables},
        databases=frozenset(),
    )

    async def fake_inventory(db, target_domain_id, *, refresh=False):
        return inventory

    calls: list[tuple[str, list[dict[str, str]]]] = []

    async def fake_write(connector, source_urn, constraints):
        calls.append((source_urn, constraints))
        return True

    monkeypatch.setattr(relation_inference.lineage_inventory, "get_inventory", fake_inventory)
    monkeypatch.setattr(relation_inference.dh, "add_foreign_key_constraints", fake_write)
    monkeypatch.setattr(
        relation_inference.SettingsService,
        "get_datahub_runtime",
        lambda self, db: SimpleNamespace(
            gms_url="http://datahub:8080",
            frontend_url="",
            token=None,
            request_timeout=90,
        ),
    )

    async def run():
        with SessionLocal() as db:
            return await relation_inference.apply_confirmed_to_datahub(db, domain_id)

    result = asyncio.run(run())
    assert result["attempted"] == 1
    assert result["applied"] == 1
    assert result["failed"] == 0
    assert result["candidates_applied"] == 1
    assert len(calls) == 1

    with SessionLocal() as db:
        assert db.get(RelationCandidateFamily, row.id).state == "applied"


# --------------------------------------------------------------------------- 异步任务


def test_start_inference_refuses_a_second_run_for_the_same_domain(domain_id, monkeypatch):
    """同域串行：两轮并行只会互相覆盖同一批候选。"""
    from app.models import RelationInferenceTask

    # 不让后台真的跑起来——这条用例只验闸门。
    monkeypatch.setattr(relation_inference.asyncio, "create_task", lambda coro: coro.close())

    db = SessionLocal()
    try:
        first = relation_inference.start_inference(db, domain_id)
        assert first.status == "queued"

        with pytest.raises(relation_inference.InferenceAlreadyRunning):
            relation_inference.start_inference(db, domain_id)

        db.query(RelationInferenceTask).filter(
            RelationInferenceTask.domain_context_id == domain_id
        ).delete()
        db.commit()
    finally:
        db.close()


def test_recover_stale_inference_tasks_unblocks_the_domain(domain_id, monkeypatch):
    """进程重启会留下永远不动的「进行中」，它会把下一次推断永久挡在门外。"""
    from app.models import RelationInferenceTask

    monkeypatch.setattr(relation_inference.asyncio, "create_task", lambda coro: coro.close())

    db = SessionLocal()
    try:
        stranded = RelationInferenceTask(
            domain_context_id=domain_id, status="running", progress=45
        )
        db.add(stranded)
        db.commit()
        stranded_id = stranded.id
    finally:
        db.close()

    assert relation_inference.recover_stale_inference_tasks() >= 1

    db = SessionLocal()
    try:
        assert db.get(RelationInferenceTask, stranded_id).status == "failed"
        # 门开了：可以再起一次
        again = relation_inference.start_inference(db, domain_id)
        assert again.status == "queued"
        db.query(RelationInferenceTask).filter(
            RelationInferenceTask.domain_context_id == domain_id
        ).delete()
        db.commit()
    finally:
        db.close()


def test_list_candidates_sorts_by_confidence(domain_id):
    db = SessionLocal()
    try:
        low = _seed(db, domain_id, family_id="f_low")
        low.confidence = 0.3
        high = _seed(db, domain_id, family_id="f_high")
        high.confidence = 0.95
        db.commit()

        rows = relation_inference.list_candidates(db, domain_id)
        assert [r.family_id for r in rows][:2] == ["f_high", "f_low"]

        only_key = relation_inference.list_candidates(
            db, domain_id, verdict="entity_key"
        )
        assert len(only_key) == 2
    finally:
        db.close()
