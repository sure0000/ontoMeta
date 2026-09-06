"""P1 overview and P2 renderable MCP result enhancements."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from app.config import settings as _settings
from app.database import SessionLocal
from app.mcp.tools import TOOL_REGISTRY, AuthContext
from app.models import (
    DomainContext,
    ObjectType,
    Ontology,
    OntologyStatus,
    RelationType,
)

AUTH = AuthContext(client_type="mcp_local", role="reader")


@pytest.fixture
def overview_fixture():
    suffix = uuid4().hex[:8]
    with SessionLocal() as db:
        domain = DomainContext(
            datahub_domain_id=f"mcp-overview-{suffix}", name=f"MCP 概览 {suffix}"
        )
        db.add(domain)
        db.flush()
        ontology = Ontology(
            domain_context_id=domain.id,
            version=1,
            status=OntologyStatus.PUBLISHED.value,
        )
        db.add(ontology)
        db.flush()
        customer = ObjectType(
            ontology_id=ontology.id,
            name=f"customer_{suffix}",
            display_name="客户",
            table_role="business_object",
            status="published",
            segment_id=None,
        )
        technical = ObjectType(
            ontology_id=ontology.id,
            name=f"audit_{suffix}",
            display_name="审计日志",
            table_role="technical",
            status="published",
        )
        db.add_all([customer, technical])
        db.flush()
        db.add(
            RelationType(
                ontology_id=ontology.id,
                name=f"customer_audit_{suffix}",
                display_name="记录",
                source_object_type_id=technical.id,
                target_object_type_id=customer.id,
                structure_type="reference",
                status="published",
            )
        )
        db.commit()
        return ontology.id


def call(name: str, arguments: dict):
    return asyncio.run(TOOL_REGISTRY[name].execute(arguments, AUTH))


def test_get_ontology_overview_returns_compact_map(overview_fixture):
    result = call("get_ontology_overview", {"ontology_id": overview_fixture})
    assert result.success, result.error
    assert result.data["ontology"]["id"] == overview_fixture
    assert result.data["object_distribution"]["by_role"]["business_object"] == 1
    assert result.data["object_distribution"]["by_role"]["technical"] == 1
    objects = result.data["business_objects"]
    assert objects["total"] == 1
    assert objects["truncated"] is False
    assert objects["items"][0]["name"].startswith("customer_")
    assert "properties" not in objects["items"][0]


def test_query_relations_can_return_mermaid(overview_fixture):
    result = call(
        "query_relations",
        {"ontology_id": overview_fixture, "include_mermaid": True},
    )
    assert result.success, result.error
    mermaid = result.data["mermaid"]
    assert mermaid.startswith("```mermaid\nflowchart LR")
    assert "记录" in mermaid
    assert "客户" in mermaid
    assert "审计日志" in mermaid


def _bypass_landing_gates(monkeypatch):
    """把「落点就绪 / 物理映射」两道闸放行。

    它们要求真实的仓部署登记，本文件的替身数据源给不出；这两道闸自己有 test_query_routing
    覆盖。这里放行它们，是为了让用例聚焦在 execute_sql 这一层。
    """
    monkeypatch.setattr("app.services.query_routing.readiness_error", lambda *a, **kw: None)
    monkeypatch.setattr("app.services.query_routing.projection_mapping", lambda *a, **kw: {})


def test_execute_sql_can_attach_vega_lite(monkeypatch):
    class _Source:
        name = "MCP 测试数仓"
        kind = "doris"
        purpose = "other"  # 非 warehouse：跳过 query_target 回执，本例只看图表 spec
        dsn_secret_ref = "sqlite:///unused"

    monkeypatch.setattr(
        "app.mcp.tools.sql.resolve_domain_data_source",
        lambda _db: _Source(),
    )
    # 执行落在共享闸门链（`services.agent_sql`）里，替身要打在服务模块上。
    monkeypatch.setattr(
        "app.services.data_app_executor.execute_sql",
        lambda **_kwargs: (
            [
                {"key": "category", "title": "category"},
                {"key": "amount", "title": "amount"},
            ],
            [{"category": "A", "amount": 3}, {"category": "B", "amount": 5}],
        ),
    )
    # 本例验的是图表 spec，不是语义证明——`t` 是个假表名，开着证明会（正确地）被拦下。
    monkeypatch.setattr(_settings, "agent_soundness", "off", raising=False)
    _bypass_landing_gates(monkeypatch)
    result = call(
        "execute_sql",
        {"sql": "SELECT category, amount FROM t", "include_vega_lite": True},
    )
    assert result.success, result.error
    spec = result.data["vega_lite"]
    assert spec["$schema"].endswith("vega-lite/v5.json")
    assert spec["mark"] == "bar"
    assert spec["encoding"]["x"]["field"] == "category"
    assert spec["encoding"]["y"]["field"] == "amount"
    assert spec["data"]["values"] == result.data["rows"]


def test_execute_sql_runs_the_shared_soundness_gate(monkeypatch):
    """引用了本体里没有的表 → 当场拒绝，而不是打到数据库上。

    这是 MCP 与对话侧的**同一道闸**（`services.agent_sql.prove_or_reject`）。此前 MCP 的
    execute_sql 只做只读校验就直连 Doris：一条引用了不存在对象的 SQL 会真的执行、报一句
    数据库错误，或者更糟——命中一张同名的物理表，返回一份没人能解释来源的结果。
    """
    class _Source:
        name = "MCP 测试数仓"
        kind = "doris"
        purpose = "other"
        dsn_secret_ref = "sqlite:///unused"

    def _boom(**_kwargs):
        raise AssertionError("语义证明未通过时不得触达数据库")

    monkeypatch.setattr("app.mcp.tools.sql.resolve_domain_data_source", lambda _db: _Source())
    monkeypatch.setattr("app.services.data_app_executor.execute_sql", _boom)
    monkeypatch.setattr(_settings, "agent_soundness", "on", raising=False)

    result = call("execute_sql", {"sql": "SELECT 天顶星科技 FROM 并不存在的表"})
    assert not result.success
    assert result.data.get("rejected") is True
    assert result.metadata.get("validation_error") is True


def test_execute_sql_without_tables_is_not_rejected(monkeypatch):
    """`SELECT 1` 读不到任何数据，没有「这张表属于本体吗」可证——不该被证明器拦掉。"""
    class _Source:
        name = "MCP 测试数仓"
        kind = "doris"
        purpose = "other"
        dsn_secret_ref = "sqlite:///unused"

    monkeypatch.setattr("app.mcp.tools.sql.resolve_domain_data_source", lambda _db: _Source())
    monkeypatch.setattr(
        "app.services.data_app_executor.execute_sql",
        lambda **_kwargs: ([{"key": "1", "title": "1"}], [{"1": 1}]),
    )
    monkeypatch.setattr(_settings, "agent_soundness", "on", raising=False)
    _bypass_landing_gates(monkeypatch)

    result = call("execute_sql", {"sql": "SELECT 1"})
    assert result.success, result.error
    assert result.data["row_count"] == 1
