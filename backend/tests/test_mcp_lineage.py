"""MCP lineage supplementation: inventory, preview, parser and approval gate."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.mcp.tools import TOOL_REGISTRY, AuthContext, tool_required_role
from app.mcp.tools import lineage as lineage_tools
from app.services.lineage_inventory import DomainInventory, InventoryTable


def _inventory() -> DomainInventory:
    return DomainInventory(
        domain_id="domain-1",
        datahub_domain_id="urn:li:domain:one",
        tables=(
            InventoryTable("urn:source", "db.source", "mysql", 0, 1),
            InventoryTable("urn:target", "db.target", "mysql", 1, 0),
        ),
        name_index={"db.source": "urn:source", "db.target": "urn:target"},
        databases=frozenset({"db"}),
    )


def test_lineage_tools_are_registered_with_safe_roles():
    assert tool_required_role(TOOL_REGISTRY["get_lineage_inventory"]) == "reader"
    assert tool_required_role(TOOL_REGISTRY["preview_lineage_supplement"]) == "editor"
    assert tool_required_role(TOOL_REGISTRY["apply_lineage_supplement"]) == "publisher"
    assert tool_required_role(TOOL_REGISTRY["apply_lineage_package"]) == "publisher"


def test_preview_manual_lineage_resolves_real_tables_and_columns(monkeypatch):
    async def fake_inventory(_db, _domain_id, **_kwargs):
        return _inventory()

    async def fake_columns(_db, urn):
        return ["id", "customer_id"] if urn == "urn:source" else ["id", "source_id"]

    monkeypatch.setattr(lineage_tools.lineage_inventory, "get_inventory", fake_inventory)
    monkeypatch.setattr(lineage_tools, "_columns_for_urn", fake_columns)

    result = asyncio.run(
        TOOL_REGISTRY["preview_lineage_supplement"].execute(
            {
                "domain_id": "domain-1",
                "edges": [
                    {
                        "source_table": "db.source",
                        "target_table": "db.target",
                        "join_keys": ["customer_id = source_id"],
                    }
                ],
            },
            AuthContext(client_type="mcp_local", role="editor"),
        )
    )

    assert result.success is True
    assert result.metadata["written"] is False
    assert result.metadata["ready"] is True
    edge = result.data["edges"][0]
    assert edge["source_urn"] == "urn:source"
    assert edge["target_urn"] == "urn:target"
    assert edge["join_keys"] == ["customer_id = source_id"]
    assert len(result.data["preview_digest"]) == 64


def test_preview_manual_lineage_blocks_unknown_column(monkeypatch):
    async def fake_inventory(_db, _domain_id, **_kwargs):
        return _inventory()

    async def fake_columns(_db, urn):
        return ["id"] if urn == "urn:source" else ["id"]

    monkeypatch.setattr(lineage_tools.lineage_inventory, "get_inventory", fake_inventory)
    monkeypatch.setattr(lineage_tools, "_columns_for_urn", fake_columns)
    result = asyncio.run(
        TOOL_REGISTRY["preview_lineage_supplement"].execute(
            {
                "domain_id": "domain-1",
                "edges": [
                    {
                        "source_table": "db.source",
                        "target_table": "db.target",
                        "join_keys": ["missing = id"],
                    }
                ],
            },
            AuthContext(client_type="mcp_local", role="editor"),
        )
    )
    assert result.success is True
    assert result.metadata["ready"] is False
    assert result.data["counts"]["blocked"] == 1
    assert "字段不存在" in result.data["edges"][0]["reason"]


def test_preview_sql_lineage_uses_parser_without_writing(monkeypatch):
    async def fake_inventory(_db, _domain_id, **_kwargs):
        return _inventory()

    monkeypatch.setattr(lineage_tools.lineage_inventory, "get_inventory", fake_inventory)
    result = asyncio.run(
        TOOL_REGISTRY["preview_sql_lineage"].execute(
            {
                "domain_id": "domain-1",
                "sql": "INSERT INTO db.target SELECT s.id FROM db.source s",
                "dialect": "mysql",
            },
            AuthContext(client_type="mcp_local", role="editor"),
        )
    )
    assert result.success is True
    assert result.data["parse"]["lineages"] == 1
    assert result.data["counts"]["ok"] == 1
    assert result.metadata["written"] is False


def test_apply_manual_lineage_requires_host_confirmation(monkeypatch):
    async def fake_inventory(_db, _domain_id, **_kwargs):
        return _inventory()

    async def fake_columns(_db, _urn):
        return ["id", "customer_id", "source_id"]

    monkeypatch.setattr(lineage_tools.lineage_inventory, "get_inventory", fake_inventory)
    monkeypatch.setattr(lineage_tools, "_columns_for_urn", fake_columns)
    monkeypatch.setattr(
        lineage_tools.SettingsService,
        "get_mcp_runtime",
        lambda _self, _db: SimpleNamespace(mcp_allow_stdio_interactive_approval=False),
    )
    result = asyncio.run(
        TOOL_REGISTRY["apply_lineage_supplement"].execute(
            {
                "domain_id": "domain-1",
                "edges": [{"source_table": "db.source", "target_table": "db.target", "join_keys": ["id = id"]}],
                "preview_digest": "0" * 64,
            },
            AuthContext(client_type="mcp_local", role="publisher", principal_id="p1"),
        )
    )
    assert result.success is False
    assert result.metadata["gate"] == "preview_stale"


def test_apply_lineage_supplement_never_accepts_remote_host_assertion(monkeypatch):
    async def fake_inventory(_db, _domain_id, **_kwargs):
        return _inventory()

    async def fake_columns(_db, _urn):
        return ["id"]

    monkeypatch.setattr(lineage_tools.lineage_inventory, "get_inventory", fake_inventory)
    monkeypatch.setattr(lineage_tools, "_columns_for_urn", fake_columns)
    monkeypatch.setattr(
        lineage_tools.SettingsService,
        "get_mcp_runtime",
        lambda _self, _db: SimpleNamespace(mcp_allow_stdio_interactive_approval=True),
    )
    preview = asyncio.run(
        TOOL_REGISTRY["preview_lineage_supplement"].execute(
            {"domain_id": "domain-1", "edges": [{"source_table": "db.source", "target_table": "db.target", "join_keys": ["id = id"]}]},
            AuthContext(client_type="mcp_local", role="editor"),
        )
    )
    result = asyncio.run(
        TOOL_REGISTRY["apply_lineage_supplement"].execute(
            {
                "domain_id": "domain-1",
                "edges": [{"source_table": "db.source", "target_table": "db.target", "join_keys": ["id = id"]}],
                "preview_digest": preview.data["preview_digest"],
                "host_confirmation": {"approved": True, "channel": "ask_user_question", "digest": preview.data["preview_digest"]},
            },
            AuthContext(client_type="mcp_remote", role="publisher", principal_id="p1"),
        )
    )
    assert result.success is False
    assert result.metadata["gate"] == "host_interactive_approval_not_allowed"
