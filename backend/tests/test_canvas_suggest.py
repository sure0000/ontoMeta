"""画布智能补录：选中若干表 → 直接给出可编辑的连线。

钉住的行为（每条都对应一个具体的失效场景）：

1. **普遍性闸门参照全域**——按选中的 20 张表算阈值会把 ``ry_bh`` 判成「太普遍」而剔掉，
   于是选得越准、连出来的线越少。这是把整域算法直接套到子集上必然踩的坑。
2. **一个族收敛成星形**，n 张表出 n-1 条线，不是 n(n-1)/2——全连通图人一样审不动。
3. **一张表在一个族里只出一列**，否则同一对表之间会画出多根语义重复的线。
4. **人已否决的族不再画**，判过的族免掉 LLM 往返：``family_id`` 是值形状的哈希，
   跨作用域稳定，所以整域推断里的表态与机器判定在这里直接生效。判 LLM 是全程唯一
   的耗时项（实测一个族 30~90 秒），而画布的用法是「加两张表再点一次」——
   每次重问就等于每次干等。
5. **对不上号的表如实报**——否则人只看到「怎么没连出线」。
6. ``not_a_key`` 的族不产出任何线。
"""

from __future__ import annotations

import json
import uuid

import anyio
import pytest

from app.database import SessionLocal
from app.models import DomainContext, RelationCandidateFamily
from app.schemas import (
    DataHubDomainBundle,
    DatasetInput,
    DomainInput,
    FieldInput,
)
from app.services import canvas_suggest, key_family_verdict


def _field(name, samples, *, distinct=100, data_type="VARCHAR(255)"):
    return FieldInput(
        name=name, data_type=data_type, unique_count=distinct, sample_values=samples
    )


def _dataset(name, fields, *, rows=1000):
    return DatasetInput(
        urn=f"urn:li:dataset:(urn:li:dataPlatform:mysql,{name},PROD)",
        name=name,
        row_count=rows,
        fields=fields,
    )


def _verdict(family_id, *, verdict="entity_key", confidence=0.9):
    return key_family_verdict.FamilyVerdict(
        family_id=family_id,
        verdict=verdict,
        entity_name="人员" if verdict != "not_a_key" else "",
        key_name="人员编号" if verdict != "not_a_key" else "",
        predicate="涉及" if verdict != "not_a_key" else "",
        confidence=confidence,
        reason="值形态是 RY + 8 位数字，跨表列名都是人员编号的拼音缩写",
    )


@pytest.fixture
def domain_id():
    db = SessionLocal()
    domain = DomainContext(
        id=str(uuid.uuid4()),
        name=f"canvas-{uuid.uuid4().hex[:6]}",
        datahub_domain_id="urn:li:domain:canvas",
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


def _wire(monkeypatch, datasets, *, judged=None):
    """把 DataHub 抓取与 LLM 判定都替掉，只测本模块自己的逻辑。"""

    async def fake_fetch(db, domain, *, refresh=False):
        return (
            DataHubDomainBundle(
                domain=DomainInput(id="x", name="x"), datasets=datasets
            ),
            True,
        )

    calls: list[list[str]] = []

    async def fake_judge(families, runtime_config=None):
        calls.append([family.id for family in families])
        return [
            (judged or {}).get(family.id) or _verdict(family.id) for family in families
        ]

    monkeypatch.setattr(canvas_suggest.datahub_bundle_cache, "fetch", fake_fetch)
    monkeypatch.setattr(canvas_suggest.key_family_verdict, "judge_families", fake_judge)
    monkeypatch.setattr(
        canvas_suggest.SettingsService, "get_llm_runtime", lambda self, db: None
    )
    return calls


# --------------------------------------------------------------------------- 作用域


def test_ubiquity_gate_uses_the_whole_domain_not_the_selection(domain_id, monkeypatch):
    """选中 12 张都带 ``ry_bh`` 的表，这个键不该因为「在选中范围里太普遍」被剔掉。

    阈值是 ``max(8, 表数 × 0.25)``：按选中的 12 张算是 8，12 > 8 → 整族消失，
    人选得越准结果越空；按全域 60 张算是 15，12 ≤ 15 → 它活着。
    """
    from app.services import key_family

    shared = [
        _dataset(f"t{i}", [_field("ry_bh", ["RY00000183", "RY00000330"])])
        for i in range(12)
    ]
    filler = [
        _dataset(f"z{i}", [_field("beizhu", ["随便写的一段说明文字"])])
        for i in range(48)
    ]
    _wire(monkeypatch, shared + filler)

    # 反证：不给参照系（即拿子集当全域）时，这个族确实会被闸门吃掉。
    assert key_family.build_key_families(shared)[0] == []

    db = SessionLocal()
    try:
        result = anyio.run(
            canvas_suggest.suggest, db, domain_id, [ds.name for ds in shared]
        )
    finally:
        db.close()

    assert result.families == 1
    assert len(result.suggestions) == 11  # 星形：12 张表 → 11 条线


def test_family_collapses_to_a_star_not_a_full_mesh(domain_id, monkeypatch):
    """4 张表共用一个键 → 3 条线，不是 6 条。"""
    datasets = [
        # 主数据端：本表内近似唯一。
        _dataset("ry_zl", [_field("ry_bh", ["RY00000183"], distinct=1000)], rows=1000),
        _dataset("aj_bl", [_field("bl_bh", ["RY00000330"], distinct=200)], rows=1000),
        _dataset("aj_ql", [_field("zbr_bh", ["RY00000412"], distinct=300)], rows=1000),
        _dataset("aj_cj", [_field("cjr_bh", ["RY00000512"], distinct=400)], rows=1000),
    ]
    _wire(monkeypatch, datasets)

    db = SessionLocal()
    try:
        result = anyio.run(
            canvas_suggest.suggest, db, domain_id, [ds.name for ds in datasets]
        )
    finally:
        db.close()

    assert len(result.suggestions) == 3
    # 轴是近唯一的那张主数据表，其余三张都指向它。
    assert {s.target_table for s in result.suggestions} == {"ry_zl"}
    assert {s.source_table for s in result.suggestions} == {"aj_bl", "aj_ql", "aj_cj"}
    assert all(s.cardinality == "many_to_one" for s in result.suggestions)
    assert all(s.key_name == "人员编号" for s in result.suggestions)


def test_one_column_per_table_per_family(domain_id, monkeypatch):
    """同一张表在族里有两列时只连一次——否则同一对表之间画出两根重复的线。"""
    datasets = [
        _dataset("ry_zl", [_field("ry_bh", ["RY00000183"], distinct=1000)], rows=1000),
        _dataset(
            "aj_bl",
            [
                _field("bl_bh", ["RY00000330"], distinct=200),
                _field("zbr_bh", ["RY00000412"], distinct=300),
            ],
            rows=1000,
        ),
    ]
    _wire(monkeypatch, datasets)

    db = SessionLocal()
    try:
        result = anyio.run(canvas_suggest.suggest, db, domain_id, ["ry_zl", "aj_bl"])
    finally:
        db.close()

    assert len(result.suggestions) == 1
    # 留下的是区分度更高的那一列。
    assert result.suggestions[0].source_column == "zbr_bh"


# --------------------------------------------------------------------------- 人工表态


def _seed(db, domain_id, family_id, *, state, verdict="entity_key"):
    row = RelationCandidateFamily(
        domain_context_id=domain_id,
        run_id="run-1",
        family_id=family_id,
        value_shape="A<2>N<8>",
        sample_values_json=json.dumps(["RY00000183"]),
        members_json="[]",
        table_count=2,
        column_count=2,
        verdict=verdict,
        entity_name="人员",
        key_name="人员编号",
        predicate="涉及",
        confidence=0.88,
        reason="整域推断时人已看过",
        state=state,
    )
    db.add(row)
    db.commit()
    return row


def _two_tables():
    return [
        _dataset("ry_zl", [_field("ry_bh", ["RY00000183"], distinct=1000)], rows=1000),
        _dataset("aj_bl", [_field("bl_bh", ["RY00000330"], distinct=200)], rows=1000),
    ]


def test_rejected_family_is_not_drawn_again(domain_id, monkeypatch):
    """人在整域推断里否过的族，不该在画布上又冒出来。"""
    datasets = _two_tables()
    calls = _wire(monkeypatch, datasets)

    db = SessionLocal()
    try:
        from app.services import key_family

        families, _ = key_family.build_key_families(datasets)
        _seed(db, domain_id, families[0].id, state="rejected")
        result = anyio.run(canvas_suggest.suggest, db, domain_id, ["ry_zl", "aj_bl"])
    finally:
        db.close()

    assert result.suggestions == []
    assert result.dismissed_families == 1
    assert calls == []  # 已表过态 → 一次模型都没问


def test_confirmed_family_skips_the_llm_and_is_labelled(domain_id, monkeypatch):
    datasets = _two_tables()
    calls = _wire(monkeypatch, datasets)

    db = SessionLocal()
    try:
        from app.services import key_family

        families, _ = key_family.build_key_families(datasets)
        _seed(db, domain_id, families[0].id, state="confirmed")
        result = anyio.run(canvas_suggest.suggest, db, domain_id, ["ry_zl", "aj_bl"])
    finally:
        db.close()

    assert len(result.suggestions) == 1
    assert result.suggestions[0].origin == canvas_suggest.ORIGIN_CONFIRMED
    assert result.suggestions[0].reason == "整域推断时人已看过"
    assert calls == []


def test_proposed_verdict_is_cached_and_reused(domain_id, monkeypatch):
    """第一次点问模型并把判定存下来，第二次点直接用缓存——这是这个交互能用的前提。

    存的成员必须覆盖**全域**的表，不是画布上这几张：这行以后被人确认时，
    ``confirmed_joins`` 展开的是整族，不该只剩当时画布上那几张。
    """
    datasets = _two_tables() + [
        _dataset("aj_ql", [_field("zbr_bh", ["RY00000412"], distinct=300)], rows=1000)
    ]
    calls = _wire(monkeypatch, datasets)

    db = SessionLocal()
    try:
        first = anyio.run(canvas_suggest.suggest, db, domain_id, ["ry_zl", "aj_bl"])
        assert len(calls) == 1  # 问了一次

        row = (
            db.query(RelationCandidateFamily)
            .filter(RelationCandidateFamily.domain_context_id == domain_id)
            .one()
        )
        assert row.state == "proposed"
        assert row.verdict == "entity_key"
        # 画布只选了 2 张，落库的成员是全域 3 张
        assert row.table_count == 3

        second = anyio.run(canvas_suggest.suggest, db, domain_id, ["ry_zl", "aj_bl"])
        assert len(calls) == 1  # 第二次没再问
        assert len(second.suggestions) == len(first.suggestions) == 1
        assert second.suggestions[0].origin == canvas_suggest.ORIGIN_KEY_FAMILY

        # refresh 是重判的出口
        anyio.run(
            lambda: canvas_suggest.suggest(db, domain_id, ["ry_zl", "aj_bl"], refresh=True)
        )
        assert len(calls) == 2
    finally:
        db.close()


def test_refresh_does_not_overturn_a_human_rejection(domain_id, monkeypatch):
    """``refresh`` 重跑的是机器判定，不是人的结论。"""
    datasets = _two_tables()
    calls = _wire(monkeypatch, datasets)

    db = SessionLocal()
    try:
        from app.services import key_family

        families, _ = key_family.build_key_families(datasets)
        _seed(db, domain_id, families[0].id, state="rejected")
        result = anyio.run(
            lambda: canvas_suggest.suggest(db, domain_id, ["ry_zl", "aj_bl"], refresh=True)
        )
    finally:
        db.close()

    assert result.suggestions == []
    assert calls == []


def test_not_a_key_family_draws_nothing(domain_id, monkeypatch):
    datasets = _two_tables()

    db = SessionLocal()
    try:
        from app.services import key_family

        families, _ = key_family.build_key_families(datasets)
        _wire(
            monkeypatch,
            datasets,
            judged={families[0].id: _verdict(families[0].id, verdict="not_a_key")},
        )
        result = anyio.run(canvas_suggest.suggest, db, domain_id, ["ry_zl", "aj_bl"])
    finally:
        db.close()

    assert result.suggestions == []
    assert result.dismissed_families == 1


# --------------------------------------------------------------------------- 表名与边界


def test_unresolvable_tables_are_reported_not_swallowed(domain_id, monkeypatch):
    datasets = _two_tables()
    _wire(monkeypatch, datasets)

    db = SessionLocal()
    try:
        result = anyio.run(
            canvas_suggest.suggest, db, domain_id, ["ry_zl", "aj_bl", "根本不存在的表"]
        )
    finally:
        db.close()

    assert result.skipped_tables == ["根本不存在的表"]
    assert sorted(result.scanned_tables) == ["aj_bl", "ry_zl"]


def test_qualified_canvas_names_match_bare_bundle_names(domain_id, monkeypatch):
    """画布节点是 ``库.表``，bundle 里是裸表名——按裸名对上，且回传时用画布那个名字。"""
    datasets = _two_tables()
    _wire(monkeypatch, datasets)

    db = SessionLocal()
    try:
        result = anyio.run(
            canvas_suggest.suggest, db, domain_id, ["jwsp.ry_zl", "jwsp.aj_bl"]
        )
    finally:
        db.close()

    assert len(result.suggestions) == 1
    edge = result.suggestions[0]
    assert edge.source_table == "jwsp.aj_bl"
    assert edge.target_table == "jwsp.ry_zl"


def test_needs_at_least_two_tables(domain_id, monkeypatch):
    _wire(monkeypatch, _two_tables())
    db = SessionLocal()
    try:
        with pytest.raises(ValueError, match="两张表"):
            anyio.run(canvas_suggest.suggest, db, domain_id, ["ry_zl"])
    finally:
        db.close()


def test_scope_is_capped(domain_id, monkeypatch):
    _wire(monkeypatch, _two_tables())
    db = SessionLocal()
    try:
        with pytest.raises(ValueError, match="最多分析"):
            anyio.run(
                canvas_suggest.suggest,
                db,
                domain_id,
                [f"t{i}" for i in range(canvas_suggest.MAX_SCOPE_TABLES + 1)],
            )
    finally:
        db.close()
