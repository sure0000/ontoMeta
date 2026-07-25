"""数据应用查询执行器（阶段 2）。

在 Binding Compiler 产出「基于本体语义的 SQL」之后，负责把它安全地在物理
数据源上执行，返回列与行。核心约束：

- **只读**：仅允许单条 SELECT / WITH…SELECT；拒绝任何 DDL/DML/多语句/危险函数。
- **强制 LIMIT**：缺失时自动追加，防止全表扫描。
- **方言适配（最小）**：针对 SQLite/DuckDB 翻译 MySQL 风格的时间函数。
- **物理映射**：DataSource.mapping_json 可将本体 name → 物理表/列名。
- **降级**：无数据源 / mock 数据源时，由调用方回退到 Mock 造数。

数据源连接串取自 DataSource.dsn_secret_ref（SQLAlchemy URL，如
sqlite:////abs/path.db、duckdb:///path、postgresql+psycopg://…）。
"""

from __future__ import annotations

import logging
import re
from typing import Any

import sqlparse
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

logger = logging.getLogger("ontometa.data_app.executor")

# 禁止出现的关键字（大小写不敏感，词边界匹配）
_FORBIDDEN = (
    "insert",
    "update",
    "delete",
    "drop",
    "alter",
    "create",
    "truncate",
    "attach",
    "detach",
    "pragma",
    "vacuum",
    "replace",
    "grant",
    "revoke",
    "call",
    "merge",
    "copy",
    "into",
)

_engine_cache: dict[str, Engine] = {}


class ExecutionError(Exception):
    """执行器可对外暴露的可读错误。"""


def _get_engine(dsn: str) -> Engine:
    engine = _engine_cache.get(dsn)
    if engine is None:
        engine = create_engine(dsn, pool_pre_ping=True)
        _engine_cache[dsn] = engine
    return engine


def is_read_only(sql: str) -> tuple[bool, str | None]:
    """校验 SQL 是否为安全的只读单条查询。返回 (ok, reason)。"""
    if not sql or not sql.strip():
        return False, "空 SQL"
    statements = [s for s in sqlparse.parse(sql) if str(s).strip()]
    if len(statements) != 1:
        return False, "仅允许单条 SQL 语句"
    stmt = statements[0]
    stmt_type = stmt.get_type()  # SELECT / INSERT / UNKNOWN…
    first_kw = None
    for token in stmt.flatten():
        if token.is_keyword and str(token).strip():
            first_kw = str(token).strip().lower()
            break
    if stmt_type != "SELECT" and first_kw not in {"select", "with"}:
        return False, f"仅允许 SELECT 查询（检测到 {stmt_type or first_kw}）"
    lowered = sql.lower()
    for kw in _FORBIDDEN:
        if re.search(rf"\b{kw}\b", lowered):
            return False, f"包含禁止的关键字：{kw}"
    return True, None


def _ensure_limit(sql: str, limit: int) -> str:
    body = sql.rstrip().rstrip(";")
    if re.search(r"\blimit\b", body, flags=re.IGNORECASE):
        return body
    return f"{body}\nLIMIT {limit}"


def _translate_dialect(sql: str, backend: str) -> str:
    """把 MySQL 风格的时间函数翻译为 SQLite/DuckDB 可执行形式（最小实现）。"""
    if backend in ("sqlite", "duckdb"):
        # DATE_SUB(CURDATE(), INTERVAL 30 DAY) -> date('now','-30 day')
        sql = re.sub(
            r"DATE_SUB\(\s*CURDATE\(\)\s*,\s*INTERVAL\s+(\d+)\s+DAY\s*\)",
            r"date('now','-\1 day')",
            sql,
            flags=re.IGNORECASE,
        )
        sql = re.sub(r"CURDATE\(\)", "date('now')", sql, flags=re.IGNORECASE)
    return sql


def _apply_mapping(sql: str, mapping: dict[str, Any] | None) -> str:
    """按 mapping_json 将本体 name 替换为物理表/列名（整词替换）。

    mapping 结构：{"tables": {ontologyName: physical}, "columns": {ontologyName: physical}}
    """
    if not mapping:
        return sql
    renames: dict[str, str] = {}
    renames.update(mapping.get("tables") or {})
    renames.update(mapping.get("columns") or {})
    for src, dst in renames.items():
        if not src or not dst or src == dst:
            continue
        sql = re.sub(rf"(?<![\w.]){re.escape(src)}(?![\w])", str(dst), sql)
    return sql


def _backend_of(dsn: str) -> str:
    prefix = dsn.split(":", 1)[0].lower()
    if prefix.startswith("sqlite"):
        return "sqlite"
    if prefix.startswith("duckdb"):
        return "duckdb"
    if prefix.startswith(("postgres", "postgresql")):
        return "postgres"
    if prefix.startswith("mysql"):
        return "mysql"
    return prefix


def execute_sql(
    *,
    dsn: str,
    sql: str,
    limit: int = 100,
    mapping: dict[str, Any] | None = None,
    timeout_seconds: int = 15,
) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    """在物理数据源上安全执行只读 SQL，返回 (columns, rows)。"""
    ok, reason = is_read_only(sql)
    if not ok:
        raise ExecutionError(f"SQL 未通过只读校验：{reason}")

    backend = _backend_of(dsn)
    prepared = _apply_mapping(sql, mapping)
    prepared = _translate_dialect(prepared, backend)
    prepared = _ensure_limit(prepared, limit)

    engine = _get_engine(dsn)
    try:
        with engine.connect() as conn:
            if backend == "postgres":
                conn.exec_driver_sql(
                    f"SET statement_timeout = {int(timeout_seconds) * 1000}"
                )
            result = conn.execute(text(prepared))
            keys = list(result.keys())
            rows = [dict(zip(keys, row)) for row in result.fetchall()]
    except Exception as exc:  # noqa: BLE001
        logger.warning("data app SQL execution failed: %s", exc)
        raise ExecutionError(f"查询执行失败：{exc}") from exc

    columns = [{"key": k, "title": k} for k in keys]
    return columns, rows
