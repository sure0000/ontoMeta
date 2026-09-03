"""只读 SQL 工具。

**只读校验与执行都复用 ``services/data_app_executor``**（Data Agent 的 run_sql 走的
同一条路），不在这里另写一份关键字黑名单：两套校验一旦分叉，宽的那套就是安全边界。

执行目标恒为「默认 Doris 数仓」——与 run_sql 同口径，由 ``resolve_domain_data_source``
fail-closed 解析；没有显式配置的默认仓就不执行，只把校验结论回给调用方。
"""

from __future__ import annotations

from app.config import settings
from app.services import data_app_executor
from app.services.chat_bi_tool_schemas import _RUN_SQL_LIMIT, _SQL_TIMEOUT_SECONDS
from app.services.data_app import resolve_domain_data_source

from . import AuthContext, ToolResult, register_tool
from ._common import as_int, session

_MAX_TIMEOUT_SECONDS = 300


@register_tool
class ExecuteSqlTool:
    """在默认 Doris 数仓执行只读 SQL"""

    name = "execute_sql"
    # 代跑 SQL 与 Data Agent 的 run_sql **同价**：手动执行端点要 publisher，若 MCP 这条
    # 路只要 reader，就成了绕过权限模型的后门。取同一份配置项，别写死。
    required_role = settings.agent_run_sql_min_role
    description = (
        "在默认 Doris 数仓执行只读 SQL 并返回结果行。\n\n"
        "限制：只允许单条 SELECT/WITH；含写操作或危险关键字一律拒绝；"
        f"未显式 LIMIT 时自动补 ORDER BY 1 + LIMIT（默认 {_RUN_SQL_LIMIT} 行，可调）。\n"
        "表名用本体对象名或已就绪的物理落点表名；不确定时先 query_objects / query_relations。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "sql": {"type": "string", "description": "SQL 查询语句（单条 SELECT/WITH）"},
            "limit": {
                "type": "integer",
                "description": f"返回行数上限（默认 {_RUN_SQL_LIMIT}）",
                "default": _RUN_SQL_LIMIT,
                "minimum": 1,
                "maximum": 1000,
            },
            "timeout": {
                "type": "integer",
                "description": f"语句超时秒数（默认 {_SQL_TIMEOUT_SECONDS}）",
                "default": _SQL_TIMEOUT_SECONDS,
                "minimum": 1,
                "maximum": _MAX_TIMEOUT_SECONDS,
            },
        },
        "required": ["sql"],
    }

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        sql = str(arguments.get("sql") or "").strip()
        if not sql:
            return ToolResult(success=False, error="缺少 sql")

        limit = as_int(arguments.get("limit"), _RUN_SQL_LIMIT, low=1, high=1000)
        timeout = as_int(
            arguments.get("timeout"), _SQL_TIMEOUT_SECONDS, low=1, high=_MAX_TIMEOUT_SECONDS
        )

        ok, reason = data_app_executor.is_read_only(sql)
        if not ok:
            return ToolResult(
                success=False,
                error=f"仅允许只读 SELECT：{reason}",
                metadata={"validation_error": True, "sql": sql},
            )

        try:
            with session() as db:
                source = resolve_domain_data_source(db)
                if source is None:
                    return ToolResult(
                        success=False,
                        error="当前未配置可执行的默认 Doris 数仓，无法执行 SQL",
                        metadata={"sql": sql, "executed": False},
                    )
                columns, rows = data_app_executor.execute_sql(
                    dsn=source.dsn_secret_ref,
                    sql=sql,
                    limit=limit,
                    timeout_seconds=timeout,
                    dialect="doris",
                )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(
                success=False,
                error=f"查询执行失败：{str(exc)[:300]}",
                metadata={"sql": sql, "executed": False},
            )

        truncated = len(rows) >= limit
        return ToolResult(
            success=True,
            data={
                "columns": columns,
                "rows": rows,
                "row_count": len(rows),
                "truncated": truncated,
            },
            metadata={
                "sql": sql,
                "datasource": source.name,
                "limit": limit,
                "timeout": timeout,
                # 截断且原 SQL 无排序 → 这是无业务序的样本，不是全集。与 run_sql 同口径。
                "sample_note": (
                    "已截断且原 SQL 未指定排序：这是一份无业务序的样本、非全集。"
                    if truncated and "order by" not in sql.lower()
                    else None
                ),
            },
        )


@register_tool
class ValidateSqlTool:
    """只读校验 SQL，不执行"""

    name = "validate_sql"
    description = "校验 SQL 是否为合法的单条只读查询。不连数据库、不执行。"
    input_schema = {
        "type": "object",
        "properties": {
            "sql": {"type": "string", "description": "要校验的 SQL 语句"},
        },
        "required": ["sql"],
    }

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        sql = str(arguments.get("sql") or "").strip()
        ok, reason = data_app_executor.is_read_only(sql)
        return ToolResult(
            success=True,
            data={"valid": ok, "reason": reason},
            metadata={"sql": sql},
        )
