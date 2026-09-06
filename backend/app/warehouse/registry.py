"""Dialect Adapter 注册表 —— 引擎知识的唯一真源。

生成器只通过这里拿 Adapter——不得直接 import 具体引擎实现，否则引擎逻辑会渗出去。
统一查询网关重构后，这里同时收编引擎的其余两类知识，查询侧不再自维护第二套映射：
- **Adapter 注册**（``get_adapter`` / ``list_engines``）：方言/DDL 生成；
- **DSN scheme 识别**（``engine_for_dsn``）：数据源连接串前缀 → 引擎名，顺序敏感
  （长前缀优先：kyuubi 在 hive 前，doris/starrocks 在 mysql 前）；
- **驱动安装提示**（``engine_driver_hint``）：缺 DBAPI 驱动时提示装哪个包；
- **会话级语句超时**（``session_timeout_statements``）：执行前给连接设的超时语句。

后两项的引擎集合比 Adapter 大：业务源（mysql / duckdb…）没有 Adapter，却一样要装驱动、
一样要设超时。所以它们是注册表里的**引擎事实表**，不是 Adapter 的渲染职责。
"""

from __future__ import annotations

import math

from app.warehouse.adapters.base import DialectAdapter
from app.warehouse.adapters.clickhouse import ClickHouseAdapter
from app.warehouse.adapters.doris import DorisAdapter
from app.warehouse.adapters.hive import HiveAdapter
from app.warehouse.adapters.iceberg import IcebergAdapter
from app.warehouse.adapters.postgres import PostgresAdapter
from app.warehouse.adapters.starrocks import StarRocksAdapter

# Doris 是唯一新建数仓引擎；历史引擎仅保留用于读取旧制品。
DEFAULT_ENGINE = "doris"

_ADAPTERS: dict[str, DialectAdapter] = {
    a.name: a
    for a in (
        HiveAdapter(),
        DorisAdapter(),
        IcebergAdapter(),
        StarRocksAdapter(),
        ClickHouseAdapter(),
        PostgresAdapter(),
    )
}
# 方言别名：Kyuubi 是 Spark SQL 网关，方言等同 Hive——复用同一实例，不另写一份。
# 别名只活在 get_adapter 解析层，不进 list_engines/list_adapters 的公开面
# （kyuubi 不是独立的可选物化引擎，与旧行为一致）。
_ENGINE_ALIASES: dict[str, str] = {"kyuubi": "hive"}

# DSN scheme 前缀 → 引擎名。顺序敏感（长前缀在前）：
# - Kyuubi/Hive 都吃 thrift 线协议，kyuubi 须在 hive 前判定；
# - Doris/StarRocks 走 MySQL 线协议，其 DSN 常写成 mysql+pymysql://，
#   但显式 doris:// / starrocks:// 应识别为自身，故须在 mysql 前判定。
_DSN_SCHEME_ENGINES: tuple[tuple[str, str], ...] = (
    ("kyuubi", "kyuubi"),
    ("hive", "hive"),
    ("starrocks", "starrocks"),
    ("doris", "doris"),
    ("clickhouse", "clickhouse"),
    ("postgres", "postgres"),
    ("postgresql", "postgres"),
    ("mysql", "mysql"),
)

# 引擎 → 缺驱动时的安装提示（Python 包名）。
# 不用 pyhive[hive] 这个 extra：它带的 sasl 是 C 扩展，Python≥3.12 编译不过
# （longintrepr.h 已移除）。pure-sasl 是纯 Python 实现，thrift-sasl 会自动用它。
_DRIVER_HINTS: dict[str, str] = {
    "mysql": "pymysql",
    "doris": "pymysql",
    "starrocks": "pymysql",
    "postgres": "psycopg2-binary",
    "hive": "pyhive thrift thrift-sasl pure-sasl",
    "kyuubi": "pyhive thrift thrift-sasl pure-sasl",
    "clickhouse": "clickhouse-sqlalchemy",
    "duckdb": "duckdb-engine",
}


class UnknownEngineError(KeyError):
    def __init__(self, engine: str):
        super().__init__(
            f"未知引擎 {engine!r}，可选：{', '.join(sorted(_ADAPTERS))}"
        )


def get_adapter(engine: str) -> DialectAdapter:
    key = _ENGINE_ALIASES.get((engine or "").lower(), (engine or "").lower())
    try:
        return _ADAPTERS[key]
    except KeyError:
        raise UnknownEngineError(engine) from None


def list_adapters() -> list[DialectAdapter]:
    return [_ADAPTERS[name] for name in sorted(_ADAPTERS)]


def list_engines() -> list[str]:
    return sorted(_ADAPTERS)


def engine_for_dsn(dsn: str) -> str | None:
    """由 DSN scheme 前缀推断引擎名；本地引擎（sqlite/duckdb）返回 None。"""
    prefix = dsn.split(":", 1)[0].lower()
    for scheme, engine in _DSN_SCHEME_ENGINES:
        if prefix.startswith(scheme):
            return engine
    return None


def engine_driver_hint(engine: str) -> str | None:
    return _DRIVER_HINTS.get((engine or "").lower())


# 引擎 → 会话级语句超时的设置语句模板。占位符 ``{ms}`` / ``{s}`` 由调用方填。
#
# **为什么必须有**：只读校验拦得住写操作，拦不住代价。一条 `SELECT pg_sleep(60)`
# 或漏了连接条件的大表 JOIN 完全合法，却能把数仓连接挂死——而 Data Agent / MCP 的
# execute_sql 正是由模型生成 SQL，跑飞是常态而非异常。上限只能由服务端设。
#
# **为什么按引擎分**：各家的旋钮名和单位都不一样，写错的后果是「设了个不存在的变量」
# 或「把 15 秒设成 15 毫秒」，两种都不会报错，只会静默失效或全量误杀：
#   - Postgres  statement_timeout   毫秒
#   - MySQL     max_execution_time  毫秒（5.7.8+，只作用于 SELECT）
#   - Doris / StarRocks  query_timeout  **秒**（MySQL 线协议但旋钮是自己的）
#   - ClickHouse  max_execution_time  **秒**（与 MySQL 同名不同单位，最容易写错的一个）
# Hive / Kyuubi 没有等价的会话级旋钮（超时在 HiveServer2 服务端配），故不在表中——
# 缺项返回空列表，调用方照常执行，不因此拒绝查询。
_SESSION_TIMEOUT_SQL: dict[str, tuple[str, ...]] = {
    "postgres": ("SET statement_timeout = {ms}",),
    "mysql": ("SET max_execution_time = {ms}",),
    "doris": ("SET query_timeout = {s}",),
    "starrocks": ("SET query_timeout = {s}",),
    "clickhouse": ("SET max_execution_time = {s}",),
}


# 没有 Adapter、但仍需引用其表名的引擎，按引号风格归到某个 Adapter 上。
# 只用于**引用标识符**这一件事，不代表方言等价——所以是这里一张窄表，而不是把它们
# 塞进 _ENGINE_ALIASES（那会让 get_adapter 把整套 DDL 渲染也一并借出去）。
#   mysql  反引号，与 Doris/Hive 同风格
#   duckdb ANSI 双引号，与 Postgres 同风格
_QUOTE_STYLE_OF: dict[str, str] = {"mysql": "doris", "duckdb": "postgres"}


def quote_table_ref(engine: str | None, ref: str) -> str:
    """按引擎的引号规则给「库.表」逐段加引号。

    源表名来自真实源，ERPNext 的 ``tabCustomer Group`` 之类带空格的名字在 ERP 域里
    占比很高——不引用产出的就是一条任何引擎都拒绝解析的 SQL。

    未知引擎回落到默认 Adapter 的风格（反引号）：拼错引号总比不引号强，后者必然失败。
    """
    name = (engine or "").lower()
    try:
        adapter = get_adapter(_QUOTE_STYLE_OF.get(name, name))
    except UnknownEngineError:
        adapter = get_adapter(DEFAULT_ENGINE)
    return adapter.quote_table_ref(ref)


def session_timeout_statements(engine: str | None, seconds: float) -> list[str]:
    """该引擎上「把本次会话的语句超时设为 ``seconds`` 秒」要执行的语句。

    引擎不支持或秒数非正 → 空列表（调用方不设超时，照常执行）。
    秒数向上取整到 1 秒：各家旋钮最小粒度不同，取 0 等于「不限时」，比不设更危险。
    """
    if not engine or seconds is None or seconds <= 0:
        return []
    templates = _SESSION_TIMEOUT_SQL.get(_ENGINE_ALIASES.get(engine.lower(), engine.lower()))
    if not templates:
        return []
    whole_seconds = max(1, int(math.ceil(float(seconds))))
    return [t.format(ms=whole_seconds * 1000, s=whole_seconds) for t in templates]
