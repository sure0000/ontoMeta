"""Built-in MCP skill pack and database override service."""

from __future__ import annotations

import hashlib
import io
import os
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy.orm import Session

from app.models.mcp_skill import McpSkill, McpSkillVersion

_ROOT = Path(__file__).resolve().parent

#: 出口契约的唯一来源。其余 skill 正文里写 ``CONTRACT_PLACEHOLDER``，下发时（MCP prompt、
#: get_playbook、导出 ZIP、安装到目录，全是同一条 ``SkillView.body``）替换成这一份的正文。
#: 此前六份 skill 各抄一遍契约：改一句要改六处，漏掉一处就只有那一类回答不守规矩，
#: 而"哪份没跟上"在界面上根本看不出来。
CONTRACT_SKILL = "ontometa-output"
CONTRACT_PLACEHOLDER = "{{OUTPUT_CONTRACT}}"

_SPECIALIZED = {
    "ontometa-discovery",
    "ontometa-query",
    "ontometa-task-plan",
    "ontometa-task-execute",
    "ontometa-admin",
    "ontometa-flow",
}

#: 技能页与导出的展示顺序：先总入口和出口契约，再按"探索 → 取数 → 规划 → 执行 → 自省"。
#: 字母序会把 admin 排在最前、把总控埋在中间——那是给机器看的顺序，不是给人读的。
_DISPLAY_ORDER = (
    "ontometa-mcp",
    "ontometa-output",
    "ontometa-flow",
    "ontometa-discovery",
    "ontometa-query",
    "ontometa-task-plan",
    "ontometa-task-execute",
    "ontometa-admin",
)

OUTPUT_CONTRACT_VERSION = "1"
OUTPUT_CONTRACT_HEADING = "## 输出格式（必须遵守）"
OUTPUT_CONTRACT = {
    "version": OUTPUT_CONTRACT_VERSION,
    "required_sections": ["结论", "结果", "依据"],
    "optional_sections": ["限制", "下一步"],
    "statuses": ["完成", "进行中", "待确认", "受阻", "失败", "无结果"],
    "max_detail_rows": 10,
}
#: 交互出口（工具返回候选、必须停下来让用户选）在总控里的必备标记。
_CHOICE_MARKERS = ("## 选择", "待确认", "回复序号")
_OUTPUT_CONTRACT_MARKERS = (
    "## 结论",
    "## 结果",
    "## 依据",
    "## 限制",
    "## 下一步",
    "状态",
    "完整 JSON",
    "最多 10 行",
)

# Existing database overrides predate the output contract.  Keep those
# overrides usable, but make sure an Agent always receives the shared minimum
# contract until an administrator saves a fully migrated version.
_OUTPUT_CONTRACT_FALLBACK = """## 输出格式（必须遵守）

最终答复默认中文，严格按以下顺序输出 Markdown：

## 结论
**状态：选择一项：完成｜进行中｜待确认｜受阻｜失败｜无结果**
一句话直接回答用户目标。

## 结果
用短段落或紧凑表格展示结果；重复明细最多 10 行，更多时写总数并标明 `truncated`。

## 依据
只列支撑结论的范围、数量、状态和必要的真实 ID；内部 ID 用反引号，名称放在前面。

## 限制
仅在截断、样本、阻断、失败或待确认时填写。

## 下一步
仅在有动作时填写，最多两项。

不要输出完整 JSON、完整 Spec、凭据或调用过程。"""


@dataclass(frozen=True)
class BuiltinSkill:
    name: str
    body: str
    frontmatter: dict[str, Any]
    digest: str


@dataclass(frozen=True)
class SkillView:
    #: ``body`` 是**合成后**下发给 Agent 的正文（含注入的出口契约）；``source_body`` 是
    #: 编辑对象（覆写原文或内置原文，仍带 {{OUTPUT_CONTRACT}} 占位符）。两者必须分开：
    #: 界面若拿合成正文去编辑，一保存就把契约固化进这份 skill，从此不再跟随总控。
    name: str
    body: str
    source_body: str
    builtin_body: str
    frontmatter: dict[str, Any]
    builtin_frontmatter: dict[str, Any]
    enabled: bool
    source: str
    builtin_digest: str
    upstream_updated: bool
    override: bool
    #: 这份 skill 的出口契约从哪来：``master``（它自己就是总控）/``inherited``（占位符引用，
    #: 跟随总控更新）/``inline``（正文里固化了一份，不再跟随）/``appended``（正文没有契约，
    #: 下发时在末尾补上总控那份）。界面据此提示"本地固化"。
    contract_source: str = "inherited"


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


def _has_output_contract(body: str) -> bool:
    """Whether a Skill body carries the complete user-facing response contract."""
    if body.count(OUTPUT_CONTRACT_HEADING) != 1:
        return False
    contract = body.split(OUTPUT_CONTRACT_HEADING, 1)[1]
    return all(marker in contract for marker in _OUTPUT_CONTRACT_MARKERS)


def contract_text(master_body: str) -> str:
    """总控 skill 里可注入的那一段：从契约标题到正文末尾。

    标题之前是"这份 skill 是干什么的"，只给读技能页的人看；注入到别的 skill 里会变成
    一段自我介绍夹在中间，故不注入。
    """
    if OUTPUT_CONTRACT_HEADING not in master_body:
        return _OUTPUT_CONTRACT_FALLBACK
    return (OUTPUT_CONTRACT_HEADING + master_body.split(OUTPUT_CONTRACT_HEADING, 1)[1]).strip()


def contract_source(name: str, body: str) -> str:
    """这份正文的出口契约从哪来（取值见 ``SkillView.contract_source``）。"""
    if name == CONTRACT_SKILL:
        return "master"
    if CONTRACT_PLACEHOLDER in body:
        return "inherited"
    if _has_output_contract(body):
        return "inline"
    return "appended"


def _compose(name: str, body: str, contract: str) -> str:
    """把生效正文合成为**真正下发给 Agent 的那一份**。

    三条路径对应 ``contract_source`` 的三种取值：占位符替换、正文已固化则原样保留
    （旧覆写先于契约总控存在，不能因为它没写占位符就把它的契约冲掉），两者都没有则补在末尾——
    最后这条是兜底：任何情况下 Agent 拿到的正文都带完整契约。
    """
    if name == CONTRACT_SKILL:
        return body
    if CONTRACT_PLACEHOLDER in body:
        return body.replace(CONTRACT_PLACEHOLDER, contract)
    if _has_output_contract(body):
        return body
    return f"{body.rstrip()}\n\n{contract}\n"


def _read_builtin(path: Path) -> BuiltinSkill:
    raw = path.read_text(encoding="utf-8")
    name = path.parent.name
    metadata = _parse(raw, name)
    if metadata.get("name") != name:
        raise ValueError(f"{name}: frontmatter name 与目录不一致")
    if name == CONTRACT_SKILL:
        if not _has_output_contract(raw):
            raise ValueError(f"{name}: 出口契约总控缺少完整契约正文")
        for marker in _CHOICE_MARKERS:
            if marker not in raw:
                raise ValueError(f"{name}: 出口契约总控缺少交互选择出口（{marker}）")
    elif CONTRACT_PLACEHOLDER not in raw:
        # 内置 skill 不许自己抄一份契约：抄了就不跟随总控，而"没跟上"在界面上看不出来。
        raise ValueError(f"{name}: 内置 Skill 必须用 {CONTRACT_PLACEHOLDER} 引用出口契约")
    return BuiltinSkill(
        name=name,
        body=raw,
        frontmatter=metadata,
        digest=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
    )


def _order_key(name: str) -> tuple[int, str]:
    return (
        _DISPLAY_ORDER.index(name) if name in _DISPLAY_ORDER else len(_DISPLAY_ORDER),
        name,
    )


def builtin_pack() -> dict[str, BuiltinSkill]:
    """Load the immutable checked-in pack without touching the database."""
    return {
        path.parent.name: _read_builtin(path)
        for path in sorted(_ROOT.glob("*/SKILL.md"))
    }


def builtin_composed(name: str) -> str:
    """内置基线的**下发正文**（出口契约已注入），不看数据库覆写。

    导出、安装和"这份 skill 到底教了什么"的回归都该对这一份：仓库里的文件只是原料，
    占位符没替换之前它不是任何 Agent 会读到的东西。
    """
    pack = builtin_pack()
    if name not in pack:
        raise ValueError(f"未知 Skill：{name}")
    return _compose(name, pack[name].body, contract_text(pack[CONTRACT_SKILL].body))


def _effective(db: Session) -> list[SkillView]:
    pack = builtin_pack()
    rows = {row.name: row for row in db.query(McpSkill).all()}

    def raw_body(name: str) -> tuple[str, bool]:
        """生效正文（未合成契约）与它是否来自覆写。"""
        row = rows.get(name)
        has_override = bool(row and row.source == "override" and (row.body_md or "").strip())
        return (row.body_md if has_override else pack[name].body), has_override

    # 契约要先定下来再合成别人：总控自己也可能被覆写，那份覆写才是当前生效的契约。
    master_raw, _ = raw_body(CONTRACT_SKILL) if CONTRACT_SKILL in pack else ("", False)
    contract = contract_text(master_raw) if master_raw else _OUTPUT_CONTRACT_FALLBACK
    result: list[SkillView] = []
    for name in sorted(pack, key=_order_key):
        builtin = pack[name]
        raw, has_override = raw_body(name)
        body = _compose(name, raw, contract)
        metadata = _parse(body, name)
        row = rows.get(name)
        result.append(
            SkillView(
                name=name,
                body=body,
                source_body=raw,
                # 上游 diff 与编辑框对的是同一层：都是**未合成**的原文，否则读者看到的
                # 差异里混着一整段注入进去的契约。
                builtin_body=builtin.body,
                frontmatter=metadata,
                builtin_frontmatter=builtin.frontmatter,
                # 出口契约总控不可停用：停掉它等于让所有回答同时失去格式约束，
                # 而界面上只表现为"某一份 skill 灰了"。
                enabled=True if name == CONTRACT_SKILL else (bool(row.enabled) if row else True),
                source="override" if has_override else "builtin",
                builtin_digest=builtin.digest,
                upstream_updated=bool(has_override and row.builtin_digest and row.builtin_digest != builtin.digest),
                override=has_override,
                contract_source=contract_source(name, raw),
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


def _effective_contract(db: Session, *, proposed: tuple[str, str] | None = None) -> str:
    """当前生效的契约正文；``proposed`` 让「正在校验总控的新版本」用新版本自己的契约。"""
    if proposed and proposed[0] == CONTRACT_SKILL:
        return contract_text(proposed[1])
    master = get_skill(db, CONTRACT_SKILL)
    return contract_text(master.body) if master else _OUTPUT_CONTRACT_FALLBACK


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
    if name == CONTRACT_SKILL:
        # 总控是契约本身：它必须自带完整契约，还必须留着交互选择那一段——
        # 少了它，"停下来让用户选"就没有任何一份 skill 规定该长什么样。
        if not _has_output_contract(body):
            missing = [marker for marker in _OUTPUT_CONTRACT_MARKERS if marker not in body]
            details = "、".join(missing[:4])
            if len(missing) > 4:
                details += "等"
            errors.append(
                f"{CONTRACT_SKILL} 是出口契约总控，正文必须包含完整契约"
                "（## 输出格式（必须遵守）、## 结论、## 结果、## 依据、## 限制、## 下一步、"
                f"状态与截断规则）；缺少：{details or '契约标题重复或位置不正确'}"
            )
        lost = [marker for marker in _CHOICE_MARKERS if marker not in body]
        if lost:
            errors.append(f"总控必须保留交互选择出口段；缺少：{'、'.join(lost)}")
    elif CONTRACT_PLACEHOLDER not in body and not _has_output_contract(body):
        errors.append(
            f"正文必须用 {CONTRACT_PLACEHOLDER} 引用出口契约"
            f"（在技能页编辑 {CONTRACT_SKILL} 可改契约本身）"
        )
    # 校验的对象是**下发给 Agent 的那一份**：占位符引用的契约不在覆写正文里，
    # 拿原文去查「## 通用底线」「工具有没有被提到」都会得出错误结论。
    composed = _compose(name, body, _effective_contract(db, proposed=(name, body)))
    if name in _SPECIALIZED and "## 通用底线" not in composed:
        errors.append("专用 Skill 必须包含「## 通用底线」段")
    gaps = skill_coverage_gaps(db, replacements={name: composed})
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
    if name == CONTRACT_SKILL and not enabled:
        raise ValueError(
            f"{CONTRACT_SKILL} 是所有回答的出口契约，不能停用；要改规则请编辑它的正文"
        )
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
    source_body = item.source_body if include_body else ""
    catalog = tool_catalog()
    mentioned = {tool["name"] for tool in catalog if tool["name"] in item.body}
    return {
        "name": item.name,
        "body": body,
        "source_body": source_body,
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
        "contract_source": item.contract_source,
        "is_output_contract": item.name == CONTRACT_SKILL,
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


#: 不许当作安装目标的路径。写进这些目录不会报错，只会把 SKILL.md 撒进系统目录里
#: 而没人找得到；家目录根同理（Agent 的 skills 目录从来不是 ``~`` 本身）。
_INSTALL_DENYLIST = {
    "/",
    "/bin",
    "/boot",
    "/dev",
    "/etc",
    "/lib",
    "/opt",
    "/private",
    "/proc",
    "/root",
    "/sbin",
    "/sys",
    "/tmp",
    "/usr",
    "/var",
    "/Applications",
    "/Library",
    "/System",
    "/Users",
    "/Volumes",
}


def resolve_install_dir(target_dir: str) -> Path:
    """把用户填的目录变成可写的绝对路径，并挡掉三类会安静出错的目标。

    这条路径是**后端进程所在主机**上的路径——安装是服务端写盘，不是浏览器下载。
    相对路径尤其危险：它落在后端进程的工作目录，而那不是填路径的人心里想的地方，
    写完还"成功"了，只是文件在别处。
    """
    raw = (target_dir or "").strip()
    if not raw:
        raise ValueError("需要目标目录")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ValueError("目标目录必须是绝对路径（相对路径会落在后端进程的工作目录）")
    resolved = Path(os.path.normpath(str(path)))
    if str(resolved) in _INSTALL_DENYLIST or resolved == Path.home():
        raise ValueError(f"拒绝写入 {resolved}；请填 Agent 真正读取 Skill 的那个目录")
    if resolved == _ROOT or _ROOT in resolved.parents:
        raise ValueError("不能安装回仓库内置 Skill 目录：那份是不可变的上游基线")
    return resolved


def install_plan(db: Session, *, target_dir: str, names: list[str] | None = None) -> dict[str, Any]:
    """安装计划：每份 Skill 会写到哪、是新建还是覆盖，写盘前先给人看。

    只落 ``<目录>/<skill-name>/SKILL.md``，目录里的其它文件一概不碰——目标目录往往
    还放着别的 Agent 技能。
    """
    resolved = resolve_install_dir(target_dir)
    skills = {item.name: item for item in list_skills(db)}
    if names:
        unknown = [name for name in names if name not in skills]
        if unknown:
            raise ValueError(f"未知 Skill：{'、'.join(unknown)}")
        selected = [skills[name] for name in names]
    else:
        selected = [item for item in skills.values() if item.enabled]
    if not selected:
        raise ValueError("没有可安装的已启用 Skill")

    items: list[dict[str, Any]] = []
    for skill in selected:
        path = resolved / skill.name / "SKILL.md"
        if path.exists():
            try:
                current = path.read_text(encoding="utf-8")
            except OSError as exc:  # noqa: PERF203 - 单文件读失败要指名道姓
                raise ValueError(f"读不到已存在的 {path}：{exc}") from exc
            action = "unchanged" if current == skill.body else "updated"
        else:
            action = "created"
        items.append(
            {
                "name": skill.name,
                "path": str(path),
                "action": action,
                "bytes": len(skill.body.encode("utf-8")),
                "enabled": skill.enabled,
            }
        )
    return {
        "target_dir": str(resolved),
        "exists": resolved.exists(),
        "items": items,
        "created": sum(1 for i in items if i["action"] == "created"),
        "updated": sum(1 for i in items if i["action"] == "updated"),
        "unchanged": sum(1 for i in items if i["action"] == "unchanged"),
    }


def install_to_dir(
    db: Session,
    *,
    target_dir: str,
    names: list[str] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """把生效 Skill 直接写进目标目录（``dry_run`` 只回计划）。

    写的是与导出 ZIP **同一条** ``SkillView.body``（含数据库覆写与合成后的出口契约），
    所以"装到目录"和"下载解压"得到的正文逐字节相同，只是省掉了解压那一步。
    """
    plan = install_plan(db, target_dir=target_dir, names=names)
    plan["dry_run"] = bool(dry_run)
    if dry_run:
        return plan
    bodies = {item.name: item.body for item in list_skills(db)}
    root = Path(plan["target_dir"])
    written: list[str] = []
    try:
        for item in plan["items"]:
            path = Path(item["path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(bodies[item["name"]], encoding="utf-8")
            written.append(str(path))
    except OSError as exc:
        # 半途失败要说清写到哪一份为止：目标目录此刻是新旧混装的。
        raise ValueError(
            f"写入失败（已写 {len(written)}/{len(plan['items'])} 份）：{exc}"
        ) from exc
    plan["exists"] = True
    plan["written"] = written
    return plan
