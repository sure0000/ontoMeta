"""P1.3 字段取值画像：堵住「谓词字面量猜错 → 返回 0 行 → 静默错答」。

被复现的缺口：``sql_soundness`` 只证明 schema 合法，不证明谓词有意义。
模型写 ``WHERE status = '已完成'`` 而库里存的是 ``Completed``——语义证明全绿、
SQL 执行成功、返回 0 行、答案「该状态无数据」，全链路无人拦截。

这里锁住修复后的契约：真实取值可读、语义类型决定策略、越权与无源优雅降级、
生成的画像 SQL 自证合法。
"""

from __future__ import annotations

import sqlite3
import uuid

import pytest

from app.database import SessionLocal
from app.models import (
    DataSource,
    DomainContext,
    EntityStatus,
    ObjectType,
    Ontology,
    OntologyStatus,
    Property,
)
from app.ontology_types import SemanticType
from app.services import column_profiler
from app.services.chat_bi import ChatBiService
from app.services.column_profiler import profile_property, strategy_for
from app.services.ontology_projection import build_projection


def _seed_ontology() -> tuple[str, str]:
    """order(订单)：status[categorical] / amount[measure] / order_date[temporal] / trace_id[technical]"""
    pub = EntityStatus.PUBLISHED.value
    uniq = uuid.uuid4().hex[:8]
    with SessionLocal() as db:
        domain = DomainContext(
            datahub_domain_id=f"urn:li:domain:prof-{uniq}", name=f"画像域-{uniq}"
        )
        db.add(domain)
        db.flush()
        onto = Ontology(
            domain_context_id=domain.id, status=OntologyStatus.PUBLISHED.value, version=1
        )
        db.add(onto)
        db.flush()
        order = ObjectType(ontology_id=onto.id, name="order", display_name="订单",
                           table_role="business_object", status=pub)
        db.add(order)
        db.flush()
        db.add_all([
            Property(object_type_id=order.id, name="status", display_name="状态",
                     semantic_type="categorical", data_type="varchar", status=pub),
            Property(object_type_id=order.id, name="amount", display_name="金额",
                     semantic_type="measure", data_type="decimal", status=pub),
            Property(object_type_id=order.id, name="order_date", display_name="下单日期",
                     semantic_type="temporal", data_type="date", status=pub),
            Property(object_type_id=order.id, name="trace_id", display_name="链路ID",
                     semantic_type="technical", data_type="varchar", status=pub),
        ])
        db.commit()
        return domain.id, onto.id


def _seed_db(tmp_path) -> str:
    """真实 sqlite 库：status 存的是英文 Completed/Draft/Cancelled，不是中文「已完成」。"""
    db_file = tmp_path / "profile.db"
    conn = sqlite3.connect(db_file)
    # order 是保留字——不加引号会直接语法错，这正是画像 SQL 必须按方言加引号的原因
    conn.execute(
        'CREATE TABLE "order" (status TEXT, amount REAL, order_date TEXT, trace_id TEXT)'
    )
    conn.executemany(
        'INSERT INTO "order" VALUES (?,?,?,?)',
        [
            ("Completed", 100.0, "2026-01-05", "t1"),
            ("Completed", 200.0, "2026-02-10", "t2"),
            ("Completed", 300.0, "2026-03-01", "t3"),
            ("Draft", 50.0, "2026-03-15", "t4"),
            ("Cancelled", None, "2026-04-01", "t5"),
        ],
    )
    conn.commit()
    conn.close()
    return f"sqlite:///{db_file}"


def _proj_and_obj(onto_id: str):
    with SessionLocal() as db:
        proj = build_projection(db, onto_id, None)
    return proj, proj.object_of("order")


@pytest.fixture(autouse=True)
def _clear_profile_cache():
    column_profiler.reset_cache()
    yield
    column_profiler.reset_cache()


# ---------------------------------------------------------------- 策略分派


def test_strategy_by_semantic_type():
    assert strategy_for(SemanticType.CATEGORICAL) == "top_values"
    assert strategy_for(SemanticType.IDENTIFIER) == "top_values"
    assert strategy_for(SemanticType.MEASURE) == "numeric_range"
    assert strategy_for(SemanticType.TEMPORAL) == "temporal_range"
    # 技术字段按语义就不该进业务查询；未知类型拿不准——都不画像
    assert strategy_for(SemanticType.TECHNICAL) == "skipped"
    assert strategy_for(SemanticType.UNKNOWN) == "skipped"


def test_technical_field_not_profiled(client, tmp_path):
    _, onto_id = _seed_ontology()
    proj, obj = _proj_and_obj(onto_id)
    p = profile_property(
        proj, obj, obj.resolve_property("trace_id"),
        dsn=_seed_db(tmp_path), backend="sqlite",
    )
    assert p.available is False
    assert p.strategy == "skipped"
    assert "技术字段" in (p.note or "")


# ---------------------------------------------------------------- 真实取值


def test_categorical_top_values_are_real(client, tmp_path):
    """核心：画像必须给出**库里真实存在的**取值，而不是本体里的想当然。"""
    _, onto_id = _seed_ontology()
    proj, obj = _proj_and_obj(onto_id)
    p = profile_property(
        proj, obj, obj.resolve_property("status"),
        dsn=_seed_db(tmp_path), backend="sqlite",
    )

    assert p.available is True
    values = [tv["value"] for tv in p.top_values]
    assert values[0] == "Completed"                 # 频次最高的排在前面
    assert set(values) == {"Completed", "Draft", "Cancelled"}
    assert "已完成" not in values                    # 模型会猜的那个中文值并不存在
    assert p.top_values[0]["freq"] == 3
    assert p.distinct_count == 3
    assert p.row_count == 5

    # 回灌给模型的 dict 必须明确「只能从这些值里选」
    d = p.to_dict()
    assert d["top_values"] == p.top_values
    assert "WHERE" in d["values_note"]


def test_measure_range_and_null_ratio(client, tmp_path):
    _, onto_id = _seed_ontology()
    proj, obj = _proj_and_obj(onto_id)
    p = profile_property(
        proj, obj, obj.resolve_property("amount"),
        dsn=_seed_db(tmp_path), backend="sqlite",
    )

    assert p.strategy == "numeric_range" and p.available is True
    assert p.min_value == 50.0 and p.max_value == 300.0
    assert p.avg_value == pytest.approx(162.5)
    assert p.row_count == 5 and p.non_null_count == 4
    assert p.null_ratio == pytest.approx(0.2)   # 1/5 为空——空值率会改变均值口径
    assert not p.top_values                      # 度量不列举取值


def test_temporal_range(client, tmp_path):
    _, onto_id = _seed_ontology()
    proj, obj = _proj_and_obj(onto_id)
    p = profile_property(
        proj, obj, obj.resolve_property("order_date"),
        dsn=_seed_db(tmp_path), backend="sqlite",
    )
    assert p.strategy == "temporal_range" and p.available is True
    assert p.min_value == "2026-01-05" and p.max_value == "2026-04-01"


def test_reserved_word_table_is_quoted(client, tmp_path):
    """``order`` 是保留字：画像 SQL 不按方言加引号会直接语法错，取不到任何值。"""
    _, onto_id = _seed_ontology()
    proj, obj = _proj_and_obj(onto_id)
    p = profile_property(
        proj, obj, obj.resolve_property("status"),
        dsn=_seed_db(tmp_path), backend="sqlite",
    )
    assert p.available is True, p.note


# ---------------------------------------------------------------- 降级


def test_no_data_source_degrades(client):
    _, onto_id = _seed_ontology()
    proj, obj = _proj_and_obj(onto_id)
    p = profile_property(proj, obj, obj.resolve_property("status"), dsn=None)
    assert p.available is False
    assert "无可执行数据源" in (p.note or "")


def test_cache_avoids_second_query(client, tmp_path, monkeypatch):
    """取值分布变化慢：同一字段第二次画像不应再打库。"""
    _, onto_id = _seed_ontology()
    proj, obj = _proj_and_obj(onto_id)
    dsn = _seed_db(tmp_path)
    kwargs = dict(dsn=dsn, backend="sqlite", scope_key=onto_id)

    first = profile_property(proj, obj, obj.resolve_property("status"), **kwargs)
    assert first.available

    from app.services import data_app_executor

    def _boom(**_kw):
        raise AssertionError("命中缓存时不应再执行 SQL")

    monkeypatch.setattr(data_app_executor, "execute_sql", _boom)
    second = profile_property(proj, obj, obj.resolve_property("status"), **kwargs)
    assert [v["value"] for v in second.top_values] == [v["value"] for v in first.top_values]


# ---------------------------------------------------------------- 权限与工具接线


def _seed_source(dsn: str) -> None:
    with SessionLocal() as db:
        db.add(DataSource(name=f"prof-{uuid.uuid4().hex[:6]}", kind="sqlite", dsn_secret_ref=dsn))
        db.commit()


def test_tool_requires_same_role_as_run_sql(client, tmp_path):
    """画像读的是真实数据，与 run_sql 同一类暴露 → 必须同一道权限闸门。"""
    domain_id, onto_id = _seed_ontology()
    _seed_source(_seed_db(tmp_path))

    with SessionLocal() as db:
        low, summary, is_error = ChatBiService()._dispatch_agent_tool(
            db, domain_id=domain_id, ontology_id=onto_id, name="profile_values",
            args={"object_id": "order", "property": "status"}, principal_role="editor",
        )
    assert is_error is False, "越权是降级不是报错"
    assert low["available"] is False
    assert "无权" in low["note"] and "权限不足" in summary


def test_tool_returns_real_values_for_publisher(client, tmp_path):
    domain_id, onto_id = _seed_ontology()
    _seed_source(_seed_db(tmp_path))

    with SessionLocal() as db:
        result, summary, is_error = ChatBiService()._dispatch_agent_tool(
            db, domain_id=domain_id, ontology_id=onto_id, name="profile_values",
            args={"object_id": "order", "property": "status"}, principal_role="publisher",
        )
    assert is_error is False, result
    assert result["available"] is True
    assert [tv["value"] for tv in result["top_values"]][0] == "Completed"
    assert "3 个取值" in summary


def test_tool_rejects_unknown_property_with_candidates(client, tmp_path):
    """字段不存在时也要给候选——与 P1.4 的修复信号取向一致。"""
    domain_id, onto_id = _seed_ontology()
    _seed_source(_seed_db(tmp_path))

    with SessionLocal() as db:
        result, _, is_error = ChatBiService()._dispatch_agent_tool(
            db, domain_id=domain_id, ontology_id=onto_id, name="profile_values",
            args={"object_id": "order", "property": "state"}, principal_role="publisher",
        )
    assert is_error is True
    assert "status" in result["available_columns"]
