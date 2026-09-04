"""Built-in MCP skill pack and database override service."""

from __future__ import annotations

import hashlib
import io
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy.orm import Session

from app.models.mcp_skill import McpSkill, McpSkillVersion

_ROOT = Path(__file__).resolve().parent
_SPECIALIZED = {
    "ontometa-discovery",
    "ontometa-query",
    "ontometa-task-plan",
    "ontometa-task-execute",
    "ontometa-admin",
}


@dataclass(frozen=True)
class BuiltinSkill:
    name: str
    body: str
    frontmatter: dict[str, Any]
    digest: str


@dataclass(frozen=True)
class SkillView:
    name: str
    body: str
    builtin_body: str
    frontmatter: dict[str, Any]
    builtin_frontmatter: dict[str, Any]
    enabled: bool
    source: str
    builtin_digest: str
    upstream_updated: bool
    override: bool


def _parse(raw: str, name: str) -> dict[str, Any]:
    if not raw.startswith("---\n"):
        raise ValueError(f"{name}: 缺少 frontmatter")
    parts = raw.split("---", 2)
    if len(parts) != 3:
        raise ValueError(f"{name}: frontmatter 不完整")
    metadata = yaml.safe_load(parts[1])
    if not isinstance(metadata, dict):
        raise ValueError(f"{name}: frontmatter 必须是对象")
    return metadata


def _read_builtin(path: Path) -> BuiltinSkill:
    raw = path.read_text(encoding="utf-8")
    name = path.parent.name
    metadata = _parse(raw, name)
    if metadata.get("name") != name:
        raise ValueError(f"{name}: frontmatter name 与目录不一致")
    return BuiltinSkill(
        name=name,
        body=raw,
        frontmatter=metadata,
        digest=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
    )


def builtin_pack() -> dict[str, BuiltinSkill]:
    """Load the immutable checked-in pack without touching the database."""
    return {
        path.parent.name: _read_builtin(path)
        for path in sorted(_ROOT.glob("*/SKILL.md"))
    }


def _effective(db: Session) -> list[SkillView]:
    pack = builtin_pack()
    rows = {row.name: row for row in db.query(McpSkill).all()}
    result: list[SkillView] = []
    for name, builtin in sorted(pack.items()):
        row = rows.get(name)
        has_override = bool(row and row.source == "override" and (row.body_md or "").strip())
        body = row.body_md if has_override else builtin.body
        metadata = _parse(body, name)
        result.append(
            SkillView(
                name=name,
                body=body,
                builtin_body=builtin.body,
                frontmatter=metadata,
                builtin_frontmatter=builtin.frontmatter,
                enabled=bool(row.enabled) if row else True,
                source="override" if has_override else "builtin",
                builtin_digest=builtin.digest,
                upstream_updated=bool(has_override and row.builtin_digest and row.builtin_digest != builtin.digest),
                override=has_override,
            )
        )
    return result


def list_skills(db: Session) -> list[SkillView]:
    return _effective(db)


def get_skill(db: Session, name: str) -> SkillView | None:
    return next((item for item in _effective(db) if item.name == name), None)


def resolve_body(db: Session, name: str) -> str | None:
    item = get_skill(db, name)
    return item.body if item else None


def skill_coverage_gaps(db: Session, *, replacements: dict[str, str] | None = None) -> list[str]:
    """Return registered tools not mentioned by any enabled effective skill."""
    from app.mcp.tools import TOOL_REGISTRY

    replacements = replacements or {}
    bodies = []
    for skill in _effective(db):
        if not skill.enabled:
            continue
        bodies.append(replacements.get(skill.name, skill.body))
    return sorted(name for name in TOOL_REGISTRY if not any(name in body for body in bodies))


def validate_skill_body(db: Session, name: str, body: str) -> list[str]:
    """Validate a proposed override and return human-readable errors."""
    pack = builtin_pack()
    if name not in pack:
        return [f"未知 Skill：{name}"]
    errors: list[str] = []
    try:
        metadata = _parse(body, name)
    except Exception as exc:  # noqa: BLE001 - return validation details to UI
        return [str(exc)]
    if metadata.get("name") != name:
        errors.append("frontmatter 的 name 必须与 Skill 名称一致")
    for key in ("user-invocable", "disable-model-invocation"):
        if not isinstance(metadata.get(key), bool):
            errors.append(f"frontmatter 的 {key} 必须是 bool")
    if "结论" not in body:
        errors.append("正文必须包含输出契约（结论）")
    if name in _SPECIALIZED and "## 通用底线" not in body:
        errors.append("专用 Skill 必须包含「## 通用底线」段")
    gaps = skill_coverage_gaps(db, replacements={name: body})
    if gaps:
        errors.append(f"以下工具没有 Skill 指引：{', '.join(gaps)}")
    return errors


def save_override(db: Session, name: str, body: str, updated_by: str | None = None) -> SkillView:
    errors = validate_skill_body(db, name, body)
    if errors:
        raise ValueError("；".join(errors))
    pack = builtin_pack()
    row = db.query(McpSkill).filter(McpSkill.name == name).one_or_none()
    if row is None:
        row = McpSkill(name=name)
        db.add(row)
    row.body_md = body
    row.source = "override"
    row.builtin_digest = pack[name].digest
    row.updated_by = updated_by
    row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    _record_version(
        db,
        name,
        body,
        action="override",
        created_by=updated_by,
        builtin_digest=pack[name].digest,
    )
    db.commit()
    db.refresh(row)
    return get_skill(db, name)  # type: ignore[return-value]


def reset_override(db: Session, name: str, updated_by: str | None = None) -> SkillView:
    row = db.query(McpSkill).filter(McpSkill.name == name).one_or_none()
    if row is None:
        item = get_skill(db, name)
        if item is None:
            raise ValueError(f"未知 Skill：{name}")
        return item
    pack = builtin_pack()
    had_override = bool(row.source == "override" and (row.body_md or "").strip())
    row.body_md = None
    row.source = "builtin"
    row.builtin_digest = None
    row.updated_by = updated_by
    row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    if had_override:
        _record_version(
            db,
            name,
            pack[name].body,
            action="restore",
            created_by=updated_by,
            builtin_digest=pack[name].digest,
        )
    db.commit()
    db.refresh(row)
    return get_skill(db, name)  # type: ignore[return-value]


def set_enabled(db: Session, name: str, enabled: bool, updated_by: str | None = None) -> SkillView:
    if name not in builtin_pack():
        raise ValueError(f"未知 Skill：{name}")
    row = db.query(McpSkill).filter(McpSkill.name == name).one_or_none()
    if row is None:
        row = McpSkill(name=name)
        db.add(row)
    row.enabled = bool(enabled)
    row.updated_by = updated_by
    row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.commit()
    db.refresh(row)
    return get_skill(db, name)  # type: ignore[return-value]


def skill_view_dict(item: SkillView, *, include_body: bool = True) -> dict[str, Any]:
    from app.mcp.introspection import tool_catalog

    body = item.body if include_body else ""
    catalog = tool_catalog()
    mentioned = {tool["name"] for tool in catalog if tool["name"] in item.body}
    return {
        "name": item.name,
        "body": body,
        "builtin_body": item.builtin_body if include_body else "",
        "frontmatter": item.frontmatter,
        "builtin_frontmatter": item.builtin_frontmatter,
        "enabled": item.enabled,
        "source": item.source,
        "builtin_digest": item.builtin_digest,
        "upstream_updated": item.upstream_updated,
        "override": item.override,
        "mentioned_tools": sorted(mentioned),
        "tool_count": len(mentioned),
    }


def _record_version(
    db: Session,
    name: str,
    body: str,
    *,
    action: str,
    created_by: str | None,
    builtin_digest: str | None,
) -> McpSkillVersion:
    latest = (
        db.query(McpSkillVersion.version)
        .filter(McpSkillVersion.skill_name == name)
        .order_by(McpSkillVersion.version.desc())
        .first()
    )
    next_version = int(latest[0] or 0) + 1 if latest else 1
    record = McpSkillVersion(
        skill_name=name,
        version=next_version,
        body_md=body,
        action=action,
        created_by=created_by,
        builtin_digest=builtin_digest,
    )
    db.add(record)
    return record


def list_versions(db: Session, name: str) -> list[dict[str, Any]]:
    if name not in builtin_pack():
        raise ValueError(f"未知 Skill：{name}")
    rows = (
        db.query(McpSkillVersion)
        .filter(McpSkillVersion.skill_name == name)
        .order_by(McpSkillVersion.version.desc())
        .all()
    )
    return [
        {
            "id": row.id,
            "skill_name": row.skill_name,
            "version": row.version,
            "body": row.body_md,
            "action": row.action,
            "created_by": row.created_by,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "builtin_digest": row.builtin_digest,
        }
        for row in rows
    ]


def restore_version(
    db: Session, name: str, version: int, updated_by: str | None = None
) -> SkillView:
    if name not in builtin_pack():
        raise ValueError(f"未知 Skill：{name}")
    row = (
        db.query(McpSkillVersion)
        .filter(McpSkillVersion.skill_name == name, McpSkillVersion.version == version)
        .one_or_none()
    )
    if row is None:
        raise ValueError(f"版本不存在：{name} v{version}")
    builtin = builtin_pack()[name]
    if row.body_md == builtin.body:
        return reset_override(db, name, updated_by=updated_by)
    return save_override(db, name, row.body_md, updated_by=updated_by)


def export_zip(db: Session, name: str | None = None) -> tuple[bytes, str]:
    """Build an installable archive from effective Skill bodies.

    ``name`` exports one Skill (including a disabled one for explicit download);
    omitting it exports every enabled Skill.  The archive contains no database
    metadata or credentials, only ``<name>/SKILL.md`` files.
    """
    if name is not None:
        skill = get_skill(db, name)
        if skill is None:
            raise ValueError(f"未知 Skill：{name}")
        selected = [skill]
        filename = f"{name}-skill.zip"
    else:
        selected = [skill for skill in list_skills(db) if skill.enabled]
        filename = "ontometa-skills.zip"
    if not selected:
        raise ValueError("没有可导出的已启用 Skill")

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for skill in selected:
            archive.writestr(f"{skill.name}/SKILL.md", skill.body)
    return buffer.getvalue(), filename
