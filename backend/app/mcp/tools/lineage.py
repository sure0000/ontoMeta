"""Blood-line supplementation tools for generic Agents.

The Web workbench remains the rich editor, while MCP exposes the same
inventory, parser and DataHub writer behind a preview/approval boundary.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from app.connectors.datahub import DataHubConnector
from app.models import LineagePackage
from app.services import lineage_inventory, lineage_package
from app.services.settings_service import SettingsService
from app.services.sql_lineage_extractor import extract

from . import AuthContext, ToolResult, register_tool
from ._common import as_int, host_confirmation_gate, session

_JOIN_KEY = re.compile(r"^\s*(?:[^.=]+\.)?([^.=\s]+)\s*=\s*(?:[^.=]+\.)?([^.=\s]+)\s*$")


def _digest(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def _columns_for_urn(db, urn: str) -> list[str]:
    connector = DataHubConnector(SettingsService().get_datahub_runtime(db))
    try:
        dataset = await connector.get_dataset_by_urn(urn)
    finally:
        await connector.aclose()
    return [str(field.name).strip() for field in (dataset.fields or []) if str(field.name).strip()]


def _parse_join_key(value: Any) -> str | None:
    if isinstance(value, dict):
        source = str(value.get("source_column") or value.get("left_column") or "").strip()
        target = str(value.get("target_column") or value.get("right_column") or "").strip()
        if source and target:
            return f"{source} = {target}"
        return None
    text = str(value or "").strip()
    if not text:
        return None
    match = _JOIN_KEY.match(text)
    if not match:
        return None
    return f"{match.group(1)} = {match.group(2)}"


async def _preview_edges(db, domain_id: str, edges: list[dict], *, validate_columns: bool) -> dict[str, Any]:
    inventory = await lineage_inventory.get_inventory(db, domain_id)
    column_cache: dict[str, set[str]] = {}
    output: list[dict[str, Any]] = []
    for item in edges:
        source = str(item.get("source_table") or "").strip()
        target = str(item.get("target_table") or "").strip()
        keys = [_parse_join_key(value) for value in (item.get("join_keys") or [])]
        keys = [key for key in keys if key]
        source_urn = inventory.resolve(source) if source else None
        target_urn = inventory.resolve(target) if target else None
        state = "ok"
        reason = None
        if not source or not target or source == target:
            state, reason = "blocked", "source_table 与 target_table 必须是两个不同的表"
        elif target_urn is None:
            state, reason = "blocked", "落点表未在 DataHub 找到"
        elif source_urn is None:
            state, reason = ("skipped", "上游表不在本域") if not inventory.in_domain(source) else ("blocked", "上游表未在 DataHub 找到")
        elif validate_columns and keys:
            for urn, side, label in ((source_urn, "source", "上游"), (target_urn, "target", "落点")):
                if urn not in column_cache:
                    column_cache[urn] = set(await _columns_for_urn(db, urn))
                names = column_cache[urn]
                column_values = []
                for key in keys:
                    left, right = [part.strip() for part in key.split("=", 1)]
                    column_values.append(left if side == "source" else right)
                missing = [column for column in column_values if column.split(".")[-1].strip('`"') not in names]
                if missing:
                    state, reason = "blocked", f"{label}字段不存在：{'、'.join(missing)}"
                    break
        if not keys:
            reason = reason or "只有表级血缘，没有可供关系推断使用的关联键"
        output.append(
            {
                "source_table": source,
                "target_table": target,
                "source_urn": source_urn,
                "target_urn": target_urn,
                "join_keys": keys,
                "state": state,
                "reason": reason,
                "target_isolated": bool(
                    target_urn
                    and any(table.urn == target_urn and table.isolated for table in inventory.tables)
                ),
            }
        )
    canonical = [
        {
            key: edge[key]
            for key in ("source_table", "target_table", "join_keys", "source_urn", "target_urn")
        }
        for edge in output
        if edge["state"] == "ok"
    ]
    return {
        "domain_id": domain_id,
        "edges": output,
        "counts": {
            "total": len(output),
            "ok": sum(edge["state"] == "ok" for edge in output),
            "blocked": sum(edge["state"] == "blocked" for edge in output),
            "skipped": sum(edge["state"] == "skipped" for edge in output),
            "isolated_targets": sum(edge["target_isolated"] for edge in output if edge["state"] == "ok"),
        },
        "preview_digest": _digest(canonical),
    }


def _package_payload(package: LineagePackage, *, include_edges: bool = False, limit: int = 100) -> dict[str, Any]:
    failures = []
    try:
        failures = json.loads(package.failures_json or "[]")
    except (TypeError, ValueError):
        failures = []
    edges = list(package.edges or [])
    data: dict[str, Any] = {
        "package_id": package.id,
        "domain_id": package.domain_context_id,
        "name": package.name,
        "kind": package.kind,
        "dialect": package.dialect,
        "status": package.status,
        "uploaded_at": package.uploaded_at,
        "scanned_at": package.scanned_at,
        "applied_at": package.applied_at,
        "sql_files": package.sql_files or 0,
        "parsed_files": package.parsed_files or 0,
        "statements": package.statements or 0,
        "failures": failures[:10],
        "edge_counts": {
            "total": len(edges),
            "ok": sum(edge.state == "ok" for edge in edges),
            "blocked": sum(edge.state == "blocked" for edge in edges),
            "skipped": sum(edge.state == "skipped" for edge in edges),
            "applied": sum(edge.applied_at is not None for edge in edges),
        },
    }
    if include_edges:
        data["edges"] = [
            {
                "id": edge.id,
                "source_table": edge.source_table,
                "target_table": edge.target_table,
                "join_key": edge.join_key,
                "source_file": edge.source_file,
                "source_urn": edge.source_urn,
                "target_urn": edge.target_urn,
                "state": edge.state,
                "reason": edge.reason,
                "applied": edge.applied_at is not None,
            }
            for edge in edges[:limit]
        ]
        data["truncated"] = len(edges) > limit
    return data


@register_tool
class GetLineageInventoryTool:
    name = "get_lineage_inventory"
    display_name = "血缘家底"
    category = "lineage"
    required_role = "reader"
    description = "读取数据域的 DataHub 血缘家底：表、上下游计数、孤岛表和覆盖率。只读。"
    input_schema = {
        "type": "object",
        "properties": {
            "domain_id": {"type": "string", "description": "数据域 ID"},
            "only_isolated": {"type": "boolean", "default": False},
            "search": {"type": "string", "description": "按库表名过滤"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 200},
            "refresh": {"type": "boolean", "default": False},
        },
        "required": ["domain_id"],
    }

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        domain_id = str(arguments.get("domain_id") or "").strip()
        if not domain_id:
            return ToolResult(success=False, error="缺少 domain_id")
        limit = as_int(arguments.get("limit"), 200, low=1, high=2000)
        try:
            with session() as db:
                inventory = await lineage_inventory.get_inventory(db, domain_id, refresh=bool(arguments.get("refresh")))
            keyword = str(arguments.get("search") or "").strip().lower()
            tables = [
                {
                    "urn": table.urn,
                    "name": table.name,
                    "platform": table.platform,
                    "upstream": table.upstream,
                    "downstream": table.downstream,
                    "isolated": table.isolated,
                }
                for table in inventory.tables
                if (not arguments.get("only_isolated") or table.isolated)
                and (not keyword or keyword in table.name.lower())
            ]
            tables.sort(key=lambda item: (not item["isolated"], item["name"].lower()))
            return ToolResult(
                success=True,
                data={
                    "domain_id": inventory.domain_id,
                    "datahub_domain_id": inventory.datahub_domain_id,
                    "databases": sorted(inventory.databases),
                    "tables": tables[:limit],
                    "counts": {
                        "total": len(inventory.tables),
                        "with_lineage": inventory.with_lineage,
                        "isolated": len(inventory.isolated_tables),
                    },
                },
                metadata={"returned": min(len(tables), limit), "total": len(tables), "truncated": len(tables) > limit},
            )
        except ValueError as exc:
            return ToolResult(success=False, error=str(exc))
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=f"读取血缘家底失败：{exc}")


@register_tool
class GetLineageColumnsTool:
    name = "get_lineage_columns"
    display_name = "血缘字段读取"
    category = "lineage"
    required_role = "reader"
    description = "读取血缘补录用的真实表字段和主键信息；表必须来自 get_lineage_inventory，不猜字段。"
    input_schema = {
        "type": "object",
        "properties": {
            "domain_id": {"type": "string"},
            "table": {"type": "string", "description": "库.表或唯一裸表名"},
            "urn": {"type": "string", "description": "也可直接传 DataHub URN"},
        },
        "required": ["domain_id"],
    }

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        domain_id = str(arguments.get("domain_id") or "").strip()
        table_name = str(arguments.get("table") or "").strip()
        urn = str(arguments.get("urn") or "").strip() or None
        if not domain_id or (not table_name and not urn):
            return ToolResult(success=False, error="需要 domain_id，以及 table 或 urn")
        try:
            with session() as db:
                inventory = await lineage_inventory.get_inventory(db, domain_id)
                if urn is None:
                    urn = inventory.resolve(table_name)
                elif urn not in {table.urn for table in inventory.tables}:
                    return ToolResult(success=False, error="urn 不属于该数据域的 DataHub 家底")
                if not urn:
                    return ToolResult(success=False, error="表未在该数据域的 DataHub 家底中找到")
                columns = await _columns_for_urn(db, urn)
            return ToolResult(success=True, data={"domain_id": domain_id, "table": table_name or urn, "urn": urn, "columns": [{"name": name} for name in columns]})
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=f"读取血缘字段失败：{exc}")


@register_tool
class PreviewLineageSupplementTool:
    name = "preview_lineage_supplement"
    display_name = "补录血缘预览"
    category = "lineage"
    required_role = "editor"
    description = "预览人工补录的表级血缘和关联键。只校验 DataHub 表/字段并返回 digest，不写本地库、不写 DataHub。"
    input_schema = {
        "type": "object",
        "properties": {
            "domain_id": {"type": "string"},
            "edges": {
                "type": "array",
                "description": "边：source_table、target_table、join_keys；join_keys 可写 source_col = target_col",
                "items": {
                    "type": "object",
                    "properties": {
                        "source_table": {"type": "string"},
                        "target_table": {"type": "string"},
                        "join_keys": {"type": "array", "items": {"type": ["string", "object"]}},
                    },
                    "required": ["source_table", "target_table"],
                },
            },
        },
        "required": ["domain_id", "edges"],
    }

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        domain_id = str(arguments.get("domain_id") or "").strip()
        edges = arguments.get("edges")
        if not domain_id or not isinstance(edges, list) or not edges:
            return ToolResult(success=False, error="需要 domain_id 和非空 edges")
        try:
            with session() as db:
                preview = await _preview_edges(db, domain_id, edges, validate_columns=True)
            return ToolResult(success=True, data=preview, metadata={"written": False, "ready": preview["counts"]["ok"] > 0 and preview["counts"]["blocked"] == 0})
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=f"预览血缘补录失败：{exc}")


@register_tool
class PreviewSqlLineageTool:
    name = "preview_sql_lineage"
    display_name = "SQL 血缘解析"
    category = "lineage"
    required_role = "editor"
    description = "解析一段 SQL 代码中的 INSERT/CTAS/VIEW 血缘，映射到当前域 DataHub URN，返回可上报边和 digest；不写库。"
    input_schema = {
        "type": "object",
        "properties": {
            "domain_id": {"type": "string"},
            "sql": {"type": "string"},
            "dialect": {"type": "string", "default": "mysql"},
        },
        "required": ["domain_id", "sql"],
    }

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        domain_id = str(arguments.get("domain_id") or "").strip()
        sql = str(arguments.get("sql") or "").strip()
        if not domain_id or not sql:
            return ToolResult(success=False, error="需要 domain_id 和 sql")
        parsed = extract(sql, dialect=str(arguments.get("dialect") or "mysql"))
        if parsed.error:
            return ToolResult(success=False, error=parsed.error, data={"statements": parsed.statements, "edges": []})
        edges = [
            {
                "source_table": source,
                "target_table": lineage.target,
                "join_keys": [key.render() for key in lineage.join_keys if key.left_table == source or key.right_table == source],
            }
            for lineage in parsed.lineages
            for source in lineage.sources
        ]
        try:
            with session() as db:
                preview = await _preview_edges(db, domain_id, edges, validate_columns=False)
            preview["parse"] = {"statements": parsed.statements, "lineages": len(parsed.lineages), "dialect": str(arguments.get("dialect") or "mysql")}
            return ToolResult(success=True, data=preview, metadata={"written": False, "ready": preview["counts"]["ok"] > 0})
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=f"预览 SQL 血缘失败：{exc}")


@register_tool
class ListLineagePackagesTool:
    name = "list_lineage_packages"
    display_name = "血缘包历史"
    category = "lineage"
    required_role = "reader"
    description = "列出某数据域的血缘代码包/画布补录历史和边统计。只读。"
    input_schema = {
        "type": "object",
        "properties": {"domain_id": {"type": "string"}, "kind": {"type": "string", "enum": ["scan", "manual", "all"], "default": "all"}, "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50}},
        "required": ["domain_id"],
    }

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        domain_id = str(arguments.get("domain_id") or "").strip()
        if not domain_id:
            return ToolResult(success=False, error="缺少 domain_id")
        limit = as_int(arguments.get("limit"), 50, low=1, high=200)
        kind = str(arguments.get("kind") or "all").strip()
        try:
            with session() as db:
                query = db.query(LineagePackage).filter(LineagePackage.domain_context_id == domain_id)
                if kind != "all":
                    query = query.filter(LineagePackage.kind == kind)
                packages = query.order_by(LineagePackage.uploaded_at.desc()).limit(limit).all()
                data = [_package_payload(package) for package in packages]
            return ToolResult(success=True, data={"packages": data}, metadata={"count": len(data), "limit": limit})
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=f"读取血缘代码包历史失败：{exc}")


@register_tool
class GetLineagePackageTool:
    name = "get_lineage_package"
    display_name = "血缘包详情"
    category = "lineage"
    required_role = "reader"
    description = "读取单个血缘代码包/画布补录的解析失败、边映射和上报状态。只读。"
    input_schema = {"type": "object", "properties": {"package_id": {"type": "string"}, "edge_limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100}}, "required": ["package_id"]}

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        package_id = str(arguments.get("package_id") or "").strip()
        if not package_id:
            return ToolResult(success=False, error="缺少 package_id")
        limit = as_int(arguments.get("edge_limit"), 100, low=1, high=500)
        with session() as db:
            package = db.get(LineagePackage, package_id)
            if package is None:
                return ToolResult(success=False, error="血缘代码包不存在")
            return ToolResult(success=True, data=_package_payload(package, include_edges=True, limit=limit))


@register_tool
class ApplyLineagePackageTool:
    name = "apply_lineage_package"
    display_name = "代码包血缘上报"
    category = "lineage"
    required_role = "publisher"
    description = "把已扫描代码包中选定的可映射边上报 DataHub。必须先展示摘要并取得宿主确认；重复上报幂等。"
    input_schema = {"type": "object", "properties": {"package_id": {"type": "string"}, "targets": {"type": "array", "items": {"type": "string"}}, "host_confirmation": {"type": "object"}}, "required": ["package_id"]}

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        package_id = str(arguments.get("package_id") or "").strip()
        targets = sorted({str(value).strip() for value in (arguments.get("targets") or []) if str(value).strip()}) or None
        if not package_id:
            return ToolResult(success=False, error="缺少 package_id")
        try:
            with session() as db:
                package = db.get(LineagePackage, package_id)
                if package is None:
                    return ToolResult(success=False, error="血缘代码包不存在")
                pending = [
                    {"id": edge.id, "source": edge.source_table, "target": edge.target_table, "join_key": edge.join_key}
                    for edge in package.edges
                    if edge.state == "ok" and edge.applied_at is None and (targets is None or edge.target_table in targets)
                ]
                digest = _digest({"package_id": package_id, "targets": targets, "edges": pending})
                blocked = host_confirmation_gate(
                    db, auth, arguments.get("host_confirmation"), digest, action="血缘代码包上报"
                )
                if blocked is not None:
                    blocked.data = {"package_id": package_id, "pending_edges": len(pending), "digest": digest}
                    return blocked
                receipt = await lineage_package.apply(db, package_id, targets=targets)
                data = {"package_id": package_id, **receipt.__dict__, "approval_digest": digest}
                if receipt.failed:
                    return ToolResult(success=False, error="血缘边部分上报失败", data=data, metadata={"partial_write": receipt.applied > 0})
                return ToolResult(success=True, data=data, metadata={"written": receipt.applied > 0})
        except ValueError as exc:
            return ToolResult(success=False, error=str(exc))
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=f"上报血缘代码包失败：{exc}")


@register_tool
class ApplyLineageSupplementTool:
    name = "apply_lineage_supplement"
    display_name = "补录血缘上报"
    category = "lineage"
    required_role = "publisher"
    description = "把人工预览通过的血缘边上报 DataHub并留本地 manual 包。必须回传同一 preview_digest 的宿主确认，不能由 Agent 自己批准。"
    input_schema = {"type": "object", "properties": {"domain_id": {"type": "string"}, "edges": {"type": "array", "items": {"type": "object"}}, "label": {"type": "string"}, "preview_digest": {"type": "string"}, "host_confirmation": {"type": "object"}}, "required": ["domain_id", "edges", "preview_digest"]}

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        domain_id = str(arguments.get("domain_id") or "").strip()
        edges = arguments.get("edges")
        if not domain_id or not isinstance(edges, list) or not edges:
            return ToolResult(success=False, error="需要 domain_id 和非空 edges")
        try:
            with session() as db:
                preview = await _preview_edges(db, domain_id, edges, validate_columns=True)
                if preview["preview_digest"] != str(arguments.get("preview_digest") or "").strip():
                    return ToolResult(success=False, error="preview_digest 与当前边不一致，请重新预览", data={"current_digest": preview["preview_digest"]}, metadata={"gate": "preview_stale"})
                if preview["counts"]["blocked"] or not preview["counts"]["ok"]:
                    return ToolResult(success=False, error="预览仍有阻断边，不能上报", data=preview, metadata={"gate": "preview_blocked"})
                blocked = host_confirmation_gate(
                    db,
                    auth,
                    arguments.get("host_confirmation"),
                    preview["preview_digest"],
                    action="人工血缘上报",
                )
                if blocked is not None:
                    blocked.data = {"preview": preview}
                    return blocked
                canonical_edges = [
                    {"source_table": edge["source_table"], "target_table": edge["target_table"], "join_keys": edge["join_keys"]}
                    for edge in preview["edges"]
                    if edge["state"] == "ok"
                ]
                receipt = await lineage_package.apply_manual(db, domain_id=domain_id, edges=canonical_edges, label=str(arguments.get("label") or "") or None)
                data = {"preview": preview, "receipt": receipt.__dict__}
                if receipt.failed:
                    return ToolResult(success=False, error="血缘边部分上报失败", data=data, metadata={"partial_write": receipt.applied > 0})
                return ToolResult(success=True, data=data, metadata={"written": receipt.applied > 0, "approval_digest": preview["preview_digest"]})
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=f"上报人工血缘失败：{exc}")


__all__ = [
    "GetLineageInventoryTool",
    "GetLineageColumnsTool",
    "PreviewLineageSupplementTool",
    "PreviewSqlLineageTool",
    "ListLineagePackagesTool",
    "GetLineagePackageTool",
    "ApplyLineagePackageTool",
    "ApplyLineageSupplementTool",
]
