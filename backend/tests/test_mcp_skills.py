"""Agent 接入控制台的 Skill pack、覆写和 MCP prompt 回归。"""

from __future__ import annotations

import asyncio
import io
import zipfile

import mcp.types as types
import pytest

from app.mcp import server
from app.mcp.skills import (
    builtin_pack,
    get_skill,
    list_skills,
    reset_override,
    list_versions,
    restore_version,
    save_override,
    skill_coverage_gaps,
    validate_skill_body,
)
from app.mcp.tools import TOOL_REGISTRY


def test_builtin_pack_is_complete(db):
    pack = builtin_pack()
    assert set(pack) == {
        "ontometa-mcp",
        "ontometa-discovery",
        "ontometa-query",
        "ontometa-task-plan",
        "ontometa-task-execute",
        "ontometa-admin",
    }
    assert len(list_skills(db)) == 6
    assert skill_coverage_gaps(db) == []


def test_skill_export_returns_effective_installable_zip(client, admin_headers, db):
    detail = get_skill(db, "ontometa-query")
    assert detail is not None
    response = client.get("/api/mcp/skills/ontometa-query/export", headers=admin_headers)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/zip")
    assert "ontometa-query-skill.zip" in response.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.namelist() == ["ontometa-query/SKILL.md"]
        assert archive.read("ontometa-query/SKILL.md").decode("utf-8") == detail.body

    all_response = client.get("/api/mcp/skills/export", headers=admin_headers)
    assert all_response.status_code == 200, all_response.text
    with zipfile.ZipFile(io.BytesIO(all_response.content)) as archive:
        assert "ontometa-query/SKILL.md" in archive.namelist()
        assert len(archive.namelist()) == 6


def test_override_and_restore_default(db):
    original = get_skill(db, "ontometa-query")
    assert original is not None
    updated = save_override(db, "ontometa-query", original.body + "\n<!-- local override -->\n")
    assert updated.source == "override"
    assert "local override" in updated.body
    assert get_skill(db, "ontometa-query").body.endswith("\n")

    restored = reset_override(db, "ontometa-query")
    assert restored.source == "builtin"
    assert restored.body == restored.builtin_body


def test_skill_versions_are_append_only_and_can_be_restored(db):
    original = get_skill(db, "ontometa-admin")
    assert original is not None
    first_body = original.body + "\n<!-- version one -->\n"
    second_body = original.body + "\n<!-- version two -->\n"
    save_override(db, "ontometa-admin", first_body, updated_by="test-v1")
    save_override(db, "ontometa-admin", second_body, updated_by="test-v2")
    versions = list_versions(db, "ontometa-admin")
    assert len(versions) >= 2
    assert versions[0]["version"] > versions[1]["version"]
    assert versions[0]["body"] == second_body
    assert versions[1]["body"] == first_body

    restored = restore_version(db, "ontometa-admin", versions[1]["version"], updated_by="test-restore")
    assert restored.source == "override"
    assert restored.body == first_body
    assert list_versions(db, "ontometa-admin")[0]["action"] == "override"
    reset_override(db, "ontometa-admin", updated_by="test-cleanup")


def test_override_validation_rejects_invalid_body(db):
    errors = validate_skill_body(db, "ontometa-query", "not markdown frontmatter")
    assert errors and "frontmatter" in errors[0]


def test_override_validation_reports_uncovered_tool(db, monkeypatch):
    class _Tool:
        name = "tool_without_skill_guidance"

    monkeypatch.setitem(TOOL_REGISTRY, _Tool.name, _Tool())
    try:
        body = builtin_pack()["ontometa-query"].body.replace("execute_sql", "execute_sql_removed")
        errors = validate_skill_body(db, "ontometa-query", body)
        assert any(_Tool.name in item for item in errors)
    finally:
        TOOL_REGISTRY.pop(_Tool.name, None)


def test_prompts_expose_enabled_skills_and_unknown_is_error(db):
    server._reset_session_auth()
    listed = asyncio.run(server.handle_list_prompts(None, None))
    assert len(listed.prompts) == 6
    assert {item.name for item in listed.prompts} >= {"ontometa-mcp", "ontometa-query"}

    prompt = asyncio.run(
        server.handle_get_prompt(
            None, types.GetPromptRequestParams(name="ontometa-query")
        )
    )
    assert prompt.messages and "compile_metric" in prompt.messages[0].content.text

    with pytest.raises(ValueError):
        asyncio.run(
            server.handle_get_prompt(
                None, types.GetPromptRequestParams(name="missing-skill")
            )
        )


def test_server_instructions_are_small_routing_only():
    assert 0 < len(server.SERVER_INSTRUCTIONS) < 2000
    for marker in ("query_ontology", "find_join_path", "profile_values", "get_ops_record"):
        assert marker in server.SERVER_INSTRUCTIONS
    assert "ontometa-query" in server.SERVER_INSTRUCTIONS
    assert "SKILL.md" not in server.SERVER_INSTRUCTIONS


def test_disabled_skill_is_not_listed(db):
    from app.mcp.skills import set_enabled

    set_enabled(db, "ontometa-admin", False)
    try:
        server._reset_session_auth()
        listed = asyncio.run(server.handle_list_prompts(None, None))
        assert "ontometa-admin" not in {item.name for item in listed.prompts}
    finally:
        set_enabled(db, "ontometa-admin", True)


def test_runtime_mcp_settings_are_database_backed_and_immediate(client, admin_headers):
    before = client.get("/api/mcp/settings", headers=admin_headers)
    assert before.status_code == 200
    original = before.json()
    try:
        updated = client.put(
            "/api/mcp/settings",
            headers=admin_headers,
            json={"mcp_rate_limit_per_minute": 9},
        )
        assert updated.status_code == 200, updated.text
        info = client.get("/api/mcp/info", headers=admin_headers).json()
        assert info["rate_limit"]["default_per_minute"] == 9
        assert info["transports"]["http"]["enabled"] is True
        assert info["transports"]["http"]["allow_anonymous"] is False
    finally:
        client.put("/api/mcp/settings", headers=admin_headers, json=original)


def test_skill_write_is_publisher_only(client, db):
    from app.services.principal_service import PrincipalService

    principal, token = PrincipalService().create(db, name="skill-reader", role="reader")
    try:
        response = client.put(
            "/api/mcp/skills/ontometa-query",
            headers={"X-Admin-Token": token},
            json={"body": builtin_pack()["ontometa-query"].body},
        )
        assert response.status_code == 403
    finally:
        PrincipalService().delete(db, principal.id)


def test_principal_mcp_access_exposes_role_matrix_and_templates(client, admin_headers, db):
    from app.services.principal_service import PrincipalService

    principal, token = PrincipalService().create(db, name="access-check", role="editor")
    try:
        response = client.get(
            f"/api/principals/{principal.id}/mcp-access", headers=admin_headers
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["allowed_count"] < body["tool_count"]
        assert "stdio_config" not in body
        assert body["http_config"]["headers"]["Authorization"].startswith("Bearer <")
    finally:
        PrincipalService().delete(db, principal.id)
