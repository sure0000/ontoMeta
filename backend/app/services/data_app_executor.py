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

import datetime
import decimal
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

# 写侧的**硬闸**：库级删除一律不许执行，任何调用方、任何理由。
#
# 只读路径（execute_sql / is_read_only）本就把 drop 整个关键字关在门外，Data Agent 碰不到。
# 但写侧（execute_write）为了物化落库必须放行 DDL——它把生成的语句原样交给 DBAPI，
# 此前**一条校验都没有**。库级删除与「建数」这件事没有任何交集：物化最多重建一张表，
# 从不需要删掉整个库；而一旦有人（或某个生成器的 bug）递进来一条 DROP DATABASE，
# 执行的代价是不可逆的整库丢失。故在此处拦死，宁可误伤也不放行。
#
# **不拦 DROP TABLE**：staging 切换（app/warehouse/adapters/base.py 的 `_with_swap`）
# 靠 `DROP TABLE IF EXISTS <staging/old>` 收尾，拦掉就等于废掉物化的原子切换。
# 表级删除的边界由物化契约管，库级删除在这里管——两码事，别混为一谈。
_DESTRUCTIVE_WRITE = re.compile(
    r"\bdrop\s+(database|schema)\b", re.IGNORECASE
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
    "postgres": "psycopg2-binary",
    # 不用 pyhive[hive] 这个 extra：它带的 sasl 是 C 扩展，Python≥3.12 编译不过
    # （longintrepr.h 已移除）。pure-sasl 是纯 Python 实现，thrift-sasl 会自动用它。
    "hive": "pyhive thrift thrift-sasl pure-sasl",
    "kyuubi": "pyhive thrift thrift-sasl pure-sasl",
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
    # 已显式带 LIMIT → 认为取数意图（含排序）由调用方掌控，原样返回。
    if re.search(r"\blimit\b", body, flags=re.IGNORECASE):
        return body
    # 自动补 LIMIT 时，若原 SQL 无 ORDER BY，追加一个稳定排序键（按第一列）。
    # 否则 `LIMIT N` 返回哪几行、何顺序由引擎自由决定（数仓并行扫描尤甚），
    # 同一份数据两次取样本会不一致——见 chat-bi「数据样例结果不同」问题。
    # ORDER BY 1 按输出列位置排序，对 `SELECT *`/聚合/UNION 均合法，保证可复现。
    if not re.search(r"\border\s+by\b", body, flags=re.IGNORECASE):
        return f"{body}\nORDER BY 1\nLIMIT {limit}"
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


# 表位置：FROM / JOIN / INTO / UPDATE / TABLE 之后紧跟的那个（或那串逗号分隔的）标识符。
# 子查询 `FROM (SELECT …)` 下一个字符是 `(`，不匹配标识符字符类，天然不会误伤；
# `FROM t alias` 只吃到 `t`，别名原样留着。
_TABLE_POSITION = re.compile(
    r"(?i)(\b(?:from|join|into|update|table)\s+)"
    r"([\w`\"\[\].]+(?:\s*,\s*[\w`\"\[\].]+)*)"
)


def _apply_mapping(sql: str, mapping: dict[str, Any] | None) -> str:
    """按 mapping_json 将本体 name 替换为物理表/列名。

    mapping 结构：{"tables": {ontologyName: physical}, "columns": {ontologyName: physical}}

    **tables 只在表位置替换，columns 才做整词替换**。原实现两者混在一起整词替换，
    在「对象名同时也是别的表的列名」时会静默改错——这在真实业务库里是常态而非例外：
    ERPNext 的 724 个对象里有 203 个（`sales_order` / `customer` / `item` …）同时是子表的
    外键列名，整词替换会把 `SELECT customer FROM tabSales_Order` 里的**列** `customer`
    也换成表名 `` `tabCustomer` ``，产出一条语法合法、语义全错的 SQL。
    报错还能被看见，静默错答不能——故按位置区分。
    """
    if not mapping:
        return sql
    tables = {
        str(k): str(v)
        for k, v in (mapping.get("tables") or {}).items()
        if k and v and k != v
    }
    if tables:
        def _sub_tables(m: re.Match) -> str:
            head, ident_list = m.group(1), m.group(2)
            parts = re.split(r"(\s*,\s*)", ident_list)
            out = [tables.get(p, p) if i % 2 == 0 else p for i, p in enumerate(parts)]
            return head + "".join(out)

        sql = _TABLE_POSITION.sub(_sub_tables, sql)
    for src, dst in (mapping.get("columns") or {}).items():
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


def _json_safe(value: Any) -> Any:
    """把驱动返回的值转成 JSON 原生标量。**必须在执行器边界做**。

    这里是物理库的值进入应用的唯一入口，往后要经过：回给模型的工具结果、`ask` 的
    响应体、图表、`analyze_result` 的统计——任何一处遇到非原生类型都会当场炸。
    MySQL 的金额列返回 `decimal.Decimal`，此前一条 `SELECT grand_total FROM sales_order`
    就能让 `POST /chat-bi/ask` 直接 500（`Object of type Decimal is not JSON serializable`），
    即「域一旦真能查，第一个金额问题就挂」。

    **Decimal 转 float 而不是 str**：下游的图表与统计要的是数，转成字符串虽然精确，
    却会让每一个金额列的图表和均值/离群统计悄悄失效——那比末位精度更贵。
    需要精确金额的场景应在 SQL 里格式化，而不是指望这里替它保留。
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    return str(value)


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
            rows = [
                {k: _json_safe(v) for k, v in zip(keys, row)}
                for row in result.fetchall()
            ]
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
        # 库级删除：**在动任何一条语句之前**就整批拒绝。放在 mapping/方言转换之后校验，
        # 是因为要拦的是真正会递给 DBAPI 的那份文本——转换前干净、转换后变成 DROP DATABASE
        # 的情况同样要挡住。整批拒绝而非跳过该条：一批语句本就是一个事务，
        # 悄悄漏执行一条会留下半套结构，比直接失败更难查。
        if _DESTRUCTIVE_WRITE.search(body):
            raise ExecutionError(
                f"拒绝执行库级删除语句（第 {idx + 1} 条）：ontoMeta 的写侧只用于建表与落数，"
                "任何情况下都不删库。如确需删库，请由 DBA 在数据库侧手工操作。"
            )
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
