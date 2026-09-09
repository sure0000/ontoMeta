"""只读 SQL 工具。

**整条闸门链复用 ``services/agent_sql``**——只读校验 → SQL 语义证明(F3) → 割接闸 →
就绪闸 → 落点映射 → 执行，与对话侧的 run_sql 逐字同一份。此前这里只做了第一步就直连
Doris，于是同一个问题在两个入口有两套安全边界；更要命的是没有落点映射，调用方只能自己
按命名规则拼物理表名，而那正是本仓反复禁止的动作。

执行目标恒为「默认 Doris 数仓」，由 ``resolve_domain_data_source`` fail-closed 解析；
没有显式配置的默认仓就不执行，只把校验结论回给调用方。
"""

from __future__ import annotations

from typing import Any

from app.config import settings
from app.services import agent_sql, data_app_executor
from app.services.agent_sql import RUN_SQL_LIMIT as _RUN_SQL_LIMIT
from app.services.agent_sql import SQL_TIMEOUT_SECONDS as _SQL_TIMEOUT_SECONDS
from app.services.datasource_service import resolve_domain_data_source

from . import AuthContext, ToolResult, register_tool
from ._common import as_int, session

_MAX_TIMEOUT_SECONDS = 300


def _vega_lite_spec(columns: list[dict[str, str]], rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """为常见的分类 + 数值结果生成小型 Vega-Lite 预览 spec。"""
    if len(columns) < 2 or not rows:
        return None
    keys = [str(column.get("key")) for column in columns if column.get("key")]
    numeric: list[str] = []
    for key in keys:
        values = [row.get(key) for row in rows if row.get(key) is not None]
        if values and all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in values):
            numeric.append(key)
    y_field = numeric[0] if numeric else keys[-1]
    x_field = next((key for key in keys if key != y_field), keys[0])
    y_type = "quantitative" if y_field in numeric else "nominal"
    mark = "bar" if y_type == "quantitative" else "point"
    return {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "description": "ontoMeta execute_sql 结果预览（最多内嵌前 100 行）",
        "data": {"values": rows[:100]},
        "mark": mark,
        "encoding": {
            "x": {"field": x_field, "type": "nominal"},
            "y": {"field": y_field, "type": y_type},
        },
    }



# 服务层已经把错误包成「查询执行失败：…」，这里再包一层就成了双前缀；SQLAlchemy 还会附上
# 完整 SQL 和一条 sqlalche.me 文档链接。这段文本既进模型上下文、也原样落进审计页给人看，
# 于是审计里出现过：
#   查询执行失败：查询执行失败：(pymysql.err.OperationalError) (1051, "Unknown table 'x'")
#   [SQL: SELECT ...] (Background on this error at: https://sqlalche.me/e/20/e3q8)
# 只留驱动自己那句话。
_NOISE_MARKERS = ("\n[SQL:", "[SQL:", "(Background on this error at:")
_DOUBLED_PREFIX = "查询执行失败："


def _clean_db_error(exc: Exception) -> str:
    return _clean_db_error_text(str(exc))


def _clean_db_error_text(text: str | None) -> str:
    """驱动噪声（SQL 回显、sqlalche.me 链接、重复前缀）剥掉，只留那句人话。"""
    text = str(text or "")
    for marker in _NOISE_MARKERS:
        head = text.split(marker, 1)[0]
        if head != text:
            text = head
    text = text.strip()
    while text.startswith(_DOUBLED_PREFIX):
        text = text[len(_DOUBLED_PREFIX) :].lstrip()
    return text[:300]


@register_tool
class ExecuteSqlTool:
    """在默认 Doris 数仓执行只读 SQL"""

    name = "execute_sql"
    display_name = "执行只读 SQL"
    category = "query"
    # 代跑 SQL 与其它 Agent SQL 入口同价：手动执行端点要 publisher，若 MCP 这条
    # 路只要 reader，就成了绕过权限模型的后门。取同一份配置项，别写死。
    required_role = settings.agent_run_sql_min_role
    description = (
        "在默认 Doris 数仓执行只读 SQL 并返回结果行。\n\n"
        "**表名写已发布本体的对象名**（如 `销售订单`），服务端按登记的落点翻译成物理表；"
        "自己按命名规则拼 `ods_xxx` 会写出并不存在的表。\n"
        "SQL 先过语义证明：引用了本体里没有的表/列、或不安全的聚合，会被当场拒绝并把可用"
        "字段回给你改（`rejected=true` + `hint`）——那不是数据库报错，是这条 SQL 本身不成立。\n"
        "落点还没就绪（同步没跑完/没登记）时返回 `executed=false` 与原因，**不要**把它读成"
        "「查到 0 行」。\n"
        f"限制：只允许单条 SELECT/WITH；未显式 LIMIT 时自动补 ORDER BY 1 + LIMIT"
        f"（默认 {_RUN_SQL_LIMIT} 行，可调）。不确定表和字段时先 query_objects / "
        "query_object_detail / find_join_path。"
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
            "include_vega_lite": {
                "type": "boolean",
                "description": "附加基于结果样本的 Vega-Lite 图表 spec",
                "default": False,
            },
            "ontology_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "限定语义证明与落点映射所依据的本体；留空＝全部已发布本体"
                    "（与「不选域的会话」同一条约定）。跨域 JOIN 时把两个本体都给上。"
                ),
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
        requested = arguments.get("ontology_ids")
        scope = [str(x).strip() for x in requested if str(x).strip()] if isinstance(requested, list) else None

        # 只读校验在**开库之前**：一条 DELETE 不该换来一次数据库连接。
        ok, reason = data_app_executor.is_read_only(sql)
        if not ok:
            return ToolResult(
                success=False,
                error=f"仅允许只读 SELECT：{reason}",
                metadata={"validation_error": True, "sql": sql},
            )

        try:
            with session() as db:
                # 角色已在服务器层按 required_role 强制过一次（与 run_sql 取同一份配置），
                # 这里再要一次 principal_role 只会把已经放行的调用重新拒掉。
                payload, summary, is_error = agent_sql.run_agent_sql(
                    db,
                    sql=sql,
                    ontology_ids=scope if scope else agent_sql.published_ontology_ids(db),
                    limit=limit,
                    timeout_seconds=timeout,
                    require_role=False,
                    # 模块级名字**在调用时**解析：测试替身掉它就能把用例钉在一个确定的
                    # 数据源上，不必真配一个默认仓。
                    resolve_source=resolve_domain_data_source,
                )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(
                success=False,
                error=f"查询执行失败：{_clean_db_error(exc)}",
                metadata={"sql": sql, "executed": False},
            )

        if is_error:
            # 语义证明拒绝 / 只读校验拒绝 / 执行报错：错误要**带着可修的线索**回去，
            # 只回一句 "失败" 的话，调用方下一步只能靠猜（通常就开始编字段）。
            return ToolResult(
                success=False,
                error=_clean_db_error_text(payload.get("reason") or payload.get("error") or summary),
                data={
                    k: payload[k]
                    for k in ("rejected", "code", "hint", "sql", "proved")
                    if k in payload
                },
                metadata={"sql": sql, "executed": False, "validation_error": True},
            )

        if not payload.get("executed"):
            # 被闸门挡下（未就绪 / 无默认仓 / 权限不足）。**必须报 success=False**：
            # 这次调用没有产生任何行，报成 success 会被读成「查到 0 行」——那是本仓
            # 反复出现的一类错答案。原因与证书一起回去，好让调用方知道下一步该做什么，
            # `blocked=true` 把它与「SQL 本身不成立」区分开。
            return ToolResult(
                success=False,
                error=payload.get("reason") or summary,
                data={
                    "executed": False,
                    "reason": payload.get("reason"),
                    "sql": payload.get("sql"),
                    "proved": payload.get("proved") or {},
                },
                metadata={"sql": sql, "executed": False, "blocked": True},
            )

        columns = payload.get("columns") or []
        rows = payload.get("rows") or []
        data: dict[str, Any] = {
            "columns": columns,
            "rows": rows,
            "row_count": payload.get("row_count", len(rows)),
            "truncated": payload.get("truncated", False),
            # 证书是「这些表/列确实是已发布本体成员」的证明结论，随结果回去，好让调用方
            # 在正文里引用字段时有据可依。
            "proved": payload.get("proved") or {},
        }
        if payload.get("query_target"):
            data["query_target"] = payload["query_target"]
        if arguments.get("include_vega_lite"):
            spec = _vega_lite_spec(columns, rows)
            if spec is not None:
                data["vega_lite"] = spec
        return ToolResult(
            success=True,
            data=data,
            metadata={
                "sql": sql,
                "datasource": payload.get("datasource"),
                "limit": limit,
                "timeout": timeout,
                "sample_note": payload.get("sample_note"),
            },
        )


@register_tool
class ValidateSqlTool:
    """只读校验 SQL，不执行"""

    name = "validate_sql"
    display_name = "SQL 校验"
    category = "query"
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
