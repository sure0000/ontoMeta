"""数据源目录工具（只读）。

提案工具要的 ``source_datasource_id`` / ``target_datasource_id`` 都是库里的真实 id。
没有一个列目录的工具，调用方只能靠「先提一个缺参的提案、从报错里读候选」——
那条路能走通，但不该是唯一的路。

**凭据不出现**：只回 id / 名称 / 类型 / 用途 / 连通状态，DSN 存的本就是
``dsn_secret_ref``，这里连它是否为空都不透出。
"""

from __future__ import annotations

from app.models.data_app import DataSource

from . import AuthContext, ToolResult, register_tool
from ._common import session

_PURPOSES = ["business_source", "warehouse"]


@register_tool
class ListDatasourcesTool:
    """列出已配置的数据源"""

    name = "list_datasources"
    description = (
        "列出已配置的数据源：业务源库（business_source）与数仓（warehouse）。"
        "建同步任务时源端取 business_source、目标端取默认 Doris 仓。不返回任何凭据。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "purpose": {
                "type": "string",
                "description": "按用途过滤",
                "enum": _PURPOSES,
            },
            "enabled_only": {
                "type": "boolean",
                "description": "只看启用的数据源",
                "default": True,
            },
        },
    }

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        purpose = (arguments.get("purpose") or "").strip() or None
        enabled_only = arguments.get("enabled_only", True)

        try:
            with session() as db:
                q = db.query(DataSource)
                if purpose:
                    q = q.filter(DataSource.purpose == purpose)
                if enabled_only:
                    q = q.filter(DataSource.enabled.is_(True))
                rows = q.order_by(DataSource.name).all()
                items = [
                    {
                        "id": s.id,
                        "name": s.name,
                        "kind": s.kind,
                        "purpose": s.purpose,
                        "enabled": s.enabled,
                        "is_default_warehouse": s.is_default_warehouse,
                        "catalog_name": s.catalog_name,
                        "status": s.status,
                        "tested_at": s.tested_at.isoformat() if s.tested_at else None,
                    }
                    for s in rows
                ]
                return ToolResult(
                    success=True,
                    data={"datasources": items},
                    metadata={
                        "count": len(items),
                        "filters": {"purpose": purpose, "enabled_only": bool(enabled_only)},
                    },
                )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=f"查询数据源失败：{exc}")
