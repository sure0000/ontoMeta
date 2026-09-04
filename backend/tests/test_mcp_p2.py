"""P1 overview and P2 renderable MCP result enhancements."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from app.database import SessionLocal
from app.mcp.tools import AuthContext, TOOL_REGISTRY
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


def test_execute_sql_can_attach_vega_lite(monkeypatch):
    class _Source:
        name = "MCP 测试数仓"
        dsn_secret_ref = "sqlite:///unused"

    monkeypatch.setattr(
        "app.mcp.tools.sql.resolve_domain_data_source",
        lambda _db: _Source(),
    )
    monkeypatch.setattr(
        "app.mcp.tools.sql.data_app_executor.execute_sql",
        lambda **_kwargs: (
            [
                {"key": "category", "title": "category"},
                {"key": "amount", "title": "amount"},
            ],
            [{"category": "A", "amount": 3}, {"category": "B", "amount": 5}],
        ),
    )
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
