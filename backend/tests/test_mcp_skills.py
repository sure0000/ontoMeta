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
from app.models.mcp_skill import McpSkill


def test_builtin_pack_is_complete(db):
    pack = builtin_pack()
    assert set(pack) == {
        "ontometa-mcp",
        "ontometa-output",
        "ontometa-flow",
        "ontometa-discovery",
        "ontometa-query",
        "ontometa-task-plan",
        "ontometa-task-execute",
        "ontometa-admin",
    }
    skills = list_skills(db)
    assert len(skills) == 8
    # 展示顺序是给人读的：总入口和出口契约在最前，不是字母序。
    assert [item.name for item in skills[:3]] == [
        "ontometa-mcp",
        "ontometa-output",
        "ontometa-flow",
    ]
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
        assert len(archive.namelist()) == 8
        # 导出的是合成正文：解压到客户端目录后，每份自带完整契约，不留待替换的占位符。
        # 总控自己除外——它的正文里在讲"别人怎么引用我"，那几处是说明文字。
        for name in archive.namelist():
            if name.startswith("ontometa-output/"):
                continue
            assert "{{OUTPUT_CONTRACT}}" not in archive.read(name).decode("utf-8")


def test_override_and_restore_default(db):
    original = get_skill(db, "ontometa-query")
    assert original is not None
    updated = save_override(
        db, "ontometa-query", original.source_body + "\n<!-- local override -->\n"
    )
    assert updated.source == "override"
    assert "local override" in updated.body
    # 改写正文仍靠占位符引用总控，不会因为一次编辑就把契约固化进来。
    assert updated.contract_source == "inherited"
    assert "{{OUTPUT_CONTRACT}}" not in updated.body
    assert "## 输出格式（必须遵守）" in updated.body
    assert get_skill(db, "ontometa-query").body.endswith("\n")

    restored = reset_override(db, "ontometa-query")
    assert restored.source == "builtin"
    # 原文回到内置基线；下发正文仍是合成后的（契约已注入）。
    assert restored.source_body == restored.builtin_body
    assert "## 输出格式（必须遵守）" in restored.body


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


def test_override_validation_requires_contract_reference(db):
    """删掉占位符又不自带契约 = 这份 skill 的回答将不受任何格式约束，必须拦下。"""
    body = builtin_pack()["ontometa-query"].body.replace("{{OUTPUT_CONTRACT}}", "")
    errors = validate_skill_body(db, "ontometa-query", body)
    assert errors and "{{OUTPUT_CONTRACT}}" in errors[0]


def test_contract_master_must_keep_full_contract_and_choice_block(db):
    from app.mcp.skills import CONTRACT_SKILL

    master = builtin_pack()[CONTRACT_SKILL].body
    errors = validate_skill_body(
        db, CONTRACT_SKILL, master.replace("## 输出格式（必须遵守）", "## 输出")
    )
    assert errors and "完整契约" in errors[0]

    errors = validate_skill_body(db, CONTRACT_SKILL, master.replace("## 选择", "## 备选"))
    assert errors and "交互选择" in errors[0]


def test_editing_the_master_changes_every_skill_at_once(db):
    """出口契约只有一份：改总控 = 所有 skill 的下发正文同时变。"""
    from app.mcp.skills import CONTRACT_SKILL

    master = get_skill(db, CONTRACT_SKILL)
    assert master is not None
    marker = "<!-- contract-propagation-probe -->"
    save_override(db, CONTRACT_SKILL, master.source_body.rstrip() + f"\n\n{marker}\n")
    try:
        for item in list_skills(db):
            assert marker in item.body, item.name
            if item.name == CONTRACT_SKILL:
                continue
            assert "{{OUTPUT_CONTRACT}}" not in item.body
            # 占位符只被替换，不该把别人的正文吃掉
            assert "## 通用底线" in item.body
    finally:
        reset_override(db, CONTRACT_SKILL)
    assert marker not in get_skill(db, "ontometa-query").body


def test_contract_master_cannot_be_disabled(db):
    from app.mcp.skills import CONTRACT_SKILL, set_enabled

    with pytest.raises(ValueError):
        set_enabled(db, CONTRACT_SKILL, False)
    assert get_skill(db, CONTRACT_SKILL).enabled is True


def test_legacy_override_receives_runtime_output_contract(db):
    original = get_skill(db, "ontometa-query")
    assert original is not None
    row = db.query(McpSkill).filter(McpSkill.name == "ontometa-query").one_or_none()
    if row is None:
        row = McpSkill(name="ontometa-query")
        db.add(row)
    row.body_md = original.body.replace("## 输出格式（必须遵守）", "## 输出")
    row.source = "override"
    row.builtin_digest = original.builtin_digest
    db.commit()
    try:
        effective = get_skill(db, "ontometa-query")
        assert effective is not None
        assert "## 输出格式（必须遵守）" in effective.body
        assert "## 结论" in effective.body and "## 下一步" in effective.body
    finally:
        reset_override(db, "ontometa-query")


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
    assert len(listed.prompts) == 8
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


def test_tool_catalog_carries_output_format_fallback():
    listed = asyncio.run(server.handle_list_tools(None, None))
    assert listed.tools
    for tool in listed.tools:
        assert "## 结论" in tool.description
        assert "最多 10 行" in tool.description
        assert "原始 JSON" in tool.description


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


# --------------------------------------------------------------------------
# 安装到目录（省掉"下载 ZIP → 找目录 → 解压"）
# --------------------------------------------------------------------------


def test_skill_install_previews_then_writes_and_is_idempotent(client, admin_headers, tmp_path):
    target = tmp_path / "agent-skills"

    preview = client.post(
        "/api/mcp/skills/install",
        headers=admin_headers,
        json={"target_dir": str(target), "dry_run": True},
    )
    assert preview.status_code == 200, preview.text
    plan = preview.json()
    assert plan["created"] == 8 and plan["updated"] == 0
    assert plan["exists"] is False
    # 预检**不写盘**：界面拿它给人看"会新建/覆盖哪几份"，此时还没做任何事。
    assert not target.exists()

    written = client.post(
        "/api/mcp/skills/install", headers=admin_headers, json={"target_dir": str(target)}
    )
    assert written.status_code == 200, written.text
    result = written.json()
    assert len(result["written"]) == 8
    body = (target / "ontometa-query" / "SKILL.md").read_text(encoding="utf-8")
    assert "## 输出格式（必须遵守）" in body
    assert "{{OUTPUT_CONTRACT}}" not in body  # 装进去的就是 Agent 直接读的正文

    again = client.post(
        "/api/mcp/skills/install",
        headers=admin_headers,
        json={"target_dir": str(target), "dry_run": True},
    ).json()
    assert again["unchanged"] == 8 and again["created"] == 0


def test_skill_install_only_touches_our_own_files(client, admin_headers, tmp_path):
    target = tmp_path / "skills"
    (target / "someone-elses-skill").mkdir(parents=True)
    keep = target / "someone-elses-skill" / "SKILL.md"
    keep.write_text("# 别人的技能", encoding="utf-8")

    client.post(
        "/api/mcp/skills/install", headers=admin_headers, json={"target_dir": str(target)}
    )
    assert keep.read_text(encoding="utf-8") == "# 别人的技能"


def test_skill_install_rejects_unsafe_targets(client, admin_headers):
    for bad in ("relative/dir", "/etc", "~", ""):
        response = client.post(
            "/api/mcp/skills/install", headers=admin_headers, json={"target_dir": bad}
        )
        assert response.status_code in (400, 422), f"{bad} → {response.status_code}"


def test_skill_install_is_publisher_only(client, db, tmp_path):
    from app.services.principal_service import PrincipalService

    principal, token = PrincipalService().create(db, name="install-editor", role="editor")
    try:
        response = client.post(
            "/api/mcp/skills/install",
            headers={"X-Admin-Token": token},
            json={"target_dir": str(tmp_path / "nope")},
        )
        assert response.status_code == 403
        assert not (tmp_path / "nope").exists()
    finally:
        PrincipalService().delete(db, principal.id)


def test_skill_install_remembers_directory(client, admin_headers, tmp_path):
    before = client.get("/api/mcp/settings", headers=admin_headers).json()
    target = tmp_path / "remembered"
    try:
        client.post(
            "/api/mcp/skills/install", headers=admin_headers, json={"target_dir": str(target)}
        )
        after = client.get("/api/mcp/settings", headers=admin_headers).json()
        assert after["mcp_skill_install_dir"] == str(target)
    finally:
        client.put(
            "/api/mcp/settings",
            headers=admin_headers,
            json={"mcp_skill_install_dir": before.get("mcp_skill_install_dir", "")},
        )
