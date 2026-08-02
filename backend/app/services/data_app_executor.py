"""数据应用查询执行器（阶段 2）。

在 Binding Compiler 产出「基于本体语义的 SQL」之后，负责把它安全地在物理
数据源上执行，返回列与行。核心约束：

- **只读**：仅允许单条 SELECT / WITH…SELECT；拒绝任何 DDL/DML/多语句/危险函数。
- **强制 LIMIT**：缺失时自动追加，防止全表扫描。
- **方言适配**：数仓引擎（hive/kyuubi/doris/starrocks/clickhouse）委托给
  ``app/warehouse`` 的 Dialect Adapter；本地分析引擎（SQLite/DuckDB）就地处理。
- **物理映射**：DataSource.mapping_json 可将本体 name → 物理表/列名。
  数仓表由本体生成后，该映射可直接由 ``services/warehouse_generator.py`` 产出——
  本体名即物理表名，无需人工维护。
- **降级**：无数据源 / mock 数据源时，由调用方回退到 Mock 造数。

数据源连接串取自 DataSource.dsn_secret_ref（SQLAlchemy URL，如
sqlite:////abs/path.db、duckdb:///path、postgresql+psycopg://、hive://host:10000/db、
kyuubi://host:10009/db…）。

注：数仓引擎的 SQLAlchemy 驱动（PyHive / clickhouse-sqlalchemy 等）**未列入
requirements.txt**——方言翻译与 backend 识别不需要它们，仅实际建连时需要，
且版本需按目标集群确认。见 requirements.txt 内的说明。
"""

from __future__ import annotations

import logging
import re
from typing import Any

import sqlparse
from sqlalchemy import create_engine, inspect as sa_inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import NoSuchModuleError

from app.warehouse import get_adapter

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

# DSN scheme 前缀 → 执行器 backend。顺序敏感：长前缀在前。
# Kyuubi 是 Spark SQL 网关，方言等同 Hive，故复用 hive Adapter。
_WAREHOUSE_SCHEMES: tuple[tuple[str, str], ...] = (
    ("kyuubi", "kyuubi"),
    ("hive", "hive"),
    ("starrocks", "starrocks"),
    ("doris", "doris"),
    ("clickhouse", "clickhouse"),
)

# 执行器 backend → app/warehouse 的 Adapter 名。
_WAREHOUSE_BACKENDS: dict[str, str] = {
    "hive": "hive",
    "kyuubi": "hive",
    "doris": "doris",
    "starrocks": "starrocks",
    "clickhouse": "clickhouse",
}


class ExecutionError(Exception):
    """执行器可对外暴露的可读错误。"""


def _get_engine(dsn: str) -> Engine:
    engine = _engine_cache.get(dsn)
    if engine is None:
        engine = create_engine(dsn, pool_pre_ping=True)
        _engine_cache[dsn] = engine
    return engine


# 数仓/数据库驱动按需自行安装（见 requirements.txt 末尾），未装时提示装哪个包。
_DRIVER_HINTS: dict[str, str] = {
    "mysql": "pymysql",
    "doris": "pymysql",
    "starrocks": "pymysql",
    "postgres": "psycopg[binary]",
    "hive": '"pyhive[hive]" thrift thrift-sasl',
    "kyuubi": '"pyhive[hive]" thrift thrift-sasl',
    "clickhouse": "clickhouse-sqlalchemy",
    "duckdb": "duckdb-engine",
}


def _engine_or_error(dsn: str) -> Engine:
    """建引擎；驱动缺失是部署问题而非代码错误，给出装哪个包的可执行提示。

    ``create_engine`` 会立即导入 DBAPI，故驱动没装时在这里就炸——不接住会以 500
    暴露 ``ModuleNotFoundError``，用户看不出要装什么。
    """
    try:
        return _get_engine(dsn)
    except (ImportError, NoSuchModuleError) as exc:
        hint = _DRIVER_HINTS.get(_backend_of(dsn))
        install = f"；请在后端环境安装：pip install {hint}" if hint else ""
        raise ExecutionError(f"缺少该数据源的数据库驱动（{exc}）{install}") from exc


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
    """方言翻译。

    数仓引擎（hive/kyuubi/doris/…）**委托给 ``app/warehouse`` 的 Dialect Adapter**，
    不在此另开分支——否则同一引擎会存在两套方言逻辑，迟早分叉。
    本函数只保留 Adapter 覆盖不到的分析型本地引擎（SQLite/DuckDB）。
    """
    engine = _WAREHOUSE_BACKENDS.get(backend)
    if engine:
        return get_adapter(engine).translate_sql(sql)

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
    # 数仓引擎须在 mysql 之前判定：Doris/StarRocks 走 MySQL 线协议，
    # 其 DSN 常写成 mysql+pymysql://，但显式 doris:// / starrocks:// 应识别为自身。
    for scheme, backend in _WAREHOUSE_SCHEMES:
        if prefix.startswith(scheme):
            return backend
    if prefix.startswith("mysql"):
        return "mysql"
    return prefix


def backend_of(dsn: str | None) -> str | None:
    """公有包装：由 DSN scheme 推断后端名（sqlite/duckdb/postgres/mysql/hive/doris/…）。

    供物化目标匹配「DataHub 平台 ↔ 已登记连接」使用；``dsn`` 为空时返回 None。
    """
    if not dsn:
        return None
    return _backend_of(dsn)


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

    engine = _engine_or_error(dsn)
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


def execute_write(
    *,
    dsn: str,
    statements: list[str],
    mapping: dict[str, Any] | None = None,
    timeout_seconds: int = 60,
) -> dict[str, Any]:
    """在物理数据源上执行写侧语句（建表 DDL / 落数 DML），单事务顺序执行。

    与 :func:`execute_sql` 相对——物化落库需要 CREATE TABLE / INSERT，故此处
    **不做只读校验、不追加 LIMIT、不取行**，改用 ``exec_driver_sql`` 把生成 SQL
    原样交给 DBAPI（避免 SQLAlchemy 把 ``:name`` / ``%`` 误当绑定参数）。

    事务性：在 ``engine.begin()`` 内顺序执行，任一条失败即整体回滚，不留半张表
    （对支持事务的 backend 如 duckdb/postgres/sqlite 成立；Hive 等无事务引擎回滚
    是 no-op，此时已建对象需靠 ``CREATE TABLE IF NOT EXISTS`` / ``INSERT OVERWRITE``
    的幂等语义兜底）。

    返回回执：``{total, executed, failed, error, per_statement}``。失败路径下
    ``executed`` 归零、先前成功的语句标 ``rolled_back``，如实反映事务已回滚。
    """
    backend = _backend_of(dsn)
    prepared: list[tuple[int, str]] = []
    for idx, raw in enumerate(statements):
        body = (raw or "").strip().rstrip(";").strip()
        if not body:
            continue
        body = _apply_mapping(body, mapping)
        body = _translate_dialect(body, backend)
        prepared.append((idx, body))

    per_statement: list[dict[str, Any]] = []
    executed = 0
    error: str | None = None

    try:
        engine = _engine_or_error(dsn)
        with engine.begin() as conn:
            if backend == "postgres":
                conn.exec_driver_sql(
                    f"SET statement_timeout = {int(timeout_seconds) * 1000}"
                )
            for idx, sql in prepared:
                try:
                    conn.exec_driver_sql(sql)
                except Exception as exc:  # noqa: BLE001
                    error = str(exc)
                    per_statement.append(
                        {"index": idx, "ok": False, "error": error, "sql": sql}
                    )
                    # 抛出以触发整事务回滚；已成功语句在下方 except 里标记回滚。
                    raise ExecutionError(error) from exc
                per_statement.append({"index": idx, "ok": True, "sql": sql})
                executed += 1
    except ExecutionError:
        pass  # error 已记录，构造回执后返回，不再上抛
    except Exception as exc:  # noqa: BLE001 —— 连接/引擎级失败（如驱动缺失）
        error = str(exc)
        logger.warning("materialization write failed: %s", exc)

    if error is not None:
        # 事务已回滚：先前 ok 的语句实际未落地。
        executed = 0
        for ps in per_statement:
            if ps.get("ok"):
                ps["ok"] = False
                ps["rolled_back"] = True

    failed = sum(1 for ps in per_statement if not ps["ok"])
    return {
        "total": len(prepared),
        "executed": executed,
        "failed": failed,
        "error": error,
        "per_statement": per_statement,
    }


# 各引擎的系统库，物化目标里没有意义，列表中直接滤掉。
_SYSTEM_SCHEMAS = {
    "information_schema",
    "performance_schema",
    "mysql",
    "sys",
    "pg_catalog",
    "pg_toast",
    "__internal_schema",  # Doris / StarRocks
    "_statistics_",
    "system",  # ClickHouse
}


def list_databases(dsn: str) -> list[str]:
    """列出目标源上的库（schema）名，供物化时选落库位置。

    走 SQLAlchemy Inspector（各方言自己知道该查什么），失败再退到 ``SHOW DATABASES``
    ——数仓引擎的方言实现参差，退化一次好过让用户面对空列表。
    """
    engine = _engine_or_error(dsn)
    names: list[str] = []
    try:
        names = list(sa_inspect(engine).get_schema_names())
    except Exception as exc:  # noqa: BLE001 —— 方言不支持 get_schema_names
        logger.info("get_schema_names unsupported, falling back to SHOW DATABASES: %s", exc)
        try:
            with engine.connect() as conn:
                names = [str(row[0]) for row in conn.exec_driver_sql("SHOW DATABASES")]
        except Exception as exc2:  # noqa: BLE001
            raise ExecutionError(f"读取库列表失败：{exc2}") from exc2
    return sorted(n for n in names if n and n.lower() not in _SYSTEM_SCHEMAS)


def list_tables(dsn: str, database: str | None = None) -> list[str]:
    """列出某个库下的表名（含视图），供物化时推荐表名与提示「已存在」。"""
    engine = _engine_or_error(dsn)
    schema = (database or "").strip() or None
    try:
        inspector = sa_inspect(engine)
        names = set(inspector.get_table_names(schema=schema))
        try:
            names |= set(inspector.get_view_names(schema=schema))
        except Exception:  # noqa: BLE001 —— 视图列举非必需，取不到就只给表
            pass
    except Exception as exc:  # noqa: BLE001
        raise ExecutionError(f"读取表列表失败：{exc}") from exc
    return sorted(names)
