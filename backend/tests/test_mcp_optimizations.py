"""Regression tests for MCP and Skill hot-path optimizations."""

from __future__ import annotations

from sqlalchemy import event

from app.database import SessionLocal, engine
from app.mcp import introspection
from app.mcp import rate_limit as rate_limit_module
from app.mcp import skills as skills_module
from app.services.settings_service import SettingsService

#: 内置 skill 的份数由目录说了算，不硬编码。
_SKILL_COUNT = len(skills_module.builtin_pack())


def _capture_sql(callback):
    statements: list[str] = []

    def before_cursor_execute(
        _conn,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", before_cursor_execute)
    try:
        result = callback()
    finally:
        event.remove(engine, "before_cursor_execute", before_cursor_execute)
    return result, statements


def test_builtin_skill_pack_is_read_once(monkeypatch):
    skills_module._builtin_pack_cached.cache_clear()
    original = skills_module._read_builtin
    reads = 0

    def counting_read(path):
        nonlocal reads
        reads += 1
        return original(path)

    monkeypatch.setattr(skills_module, "_read_builtin", counting_read)
    first = skills_module.builtin_pack()
    second = skills_module.builtin_pack()

    assert first.keys() == second.keys()
    assert reads == len(first)


def test_rate_limiter_reuses_runtime_config_for_concurrent_calls(db):
    service = SettingsService()
    original = service.get_mcp_settings(db)
    try:
        service.update_mcp_settings(
            db,
            {
                "mcp_rate_limit_per_minute": 10,
                "mcp_execute_sql_rate_limit_per_minute": 3,
            },
        )
        rate_limit_module.reset_rate_limit()

        _, statements = _capture_sql(
            lambda: (
                rate_limit_module.check_rate_limit("query_objects", now=0.0),
                rate_limit_module.check_rate_limit("query_objects", now=0.1),
            )
        )
        config_reads = [
            statement
            for statement in statements
            if "dependency_components" in statement
        ]
        assert len(config_reads) == 1
    finally:
        service.update_mcp_settings(db, original)
        rate_limit_module.reset_rate_limit()


def test_stats_uses_one_aggregate_for_totals(db):
    _, statements = _capture_sql(
        lambda: introspection.compute_stats(db, window_minutes=60)
    )

    audit_queries = [
        statement for statement in statements if "mcp_audit_logs" in statement
    ]
    # summary, duration distribution, tool/role/error groups, trend, distinct
    # principals and last-call timestamp.  The five counters are one query.
    assert len(audit_queries) == 8
    assert any("sum(CAST" in statement or "sum(CASE" in statement for statement in audit_queries)


def test_skill_install_rejects_symlink_to_denylisted_directory(tmp_path):
    link = tmp_path / "agent-skills"
    link.symlink_to("/etc", target_is_directory=True)

    try:
        skills_module.resolve_install_dir(str(link))
    except ValueError as exc:
        assert "拒绝写入" in str(exc)
    else:
        raise AssertionError("symlinked denylisted path must be rejected")


def test_skill_install_does_not_rewrite_unchanged_files(tmp_path):
    target = tmp_path / "skills"
    with SessionLocal() as db:
        first = skills_module.install_to_dir(db, target_dir=str(target))
        second = skills_module.install_to_dir(db, target_dir=str(target))

    assert len(first["written"]) == first["created"] == _SKILL_COUNT
    assert second["written"] == []
    assert second["unchanged"] == _SKILL_COUNT


def test_skill_install_rejects_symlinked_skill_files(tmp_path):
    target = tmp_path / "skills"
    skill_dir = target / "ontometa-query"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").symlink_to(tmp_path / "outside.md")

    with SessionLocal() as db:
        try:
            skills_module.install_plan(db, target_dir=str(target), names=["ontometa-query"])
        except ValueError as exc:
            assert "symlink" in str(exc)
        else:
            raise AssertionError("symlinked Skill file must be rejected")


def test_skill_catalog_returns_the_registry_snapshot(client, admin_headers):
    response = client.get("/api/mcp/skills", headers=admin_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["tools"]) >= 30
    assert {tool["name"] for tool in body["tools"]} >= {
        "get_playbook",
        "execute_sql",
    }
