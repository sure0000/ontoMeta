"""Dialect Adapter 注册表 —— 引擎知识的唯一真源。

生成器只通过这里拿 Adapter——不得直接 import 具体引擎实现，否则引擎逻辑会渗出去。
统一查询网关重构后，这里同时收编引擎的其余两类知识，查询侧不再自维护第二套映射：
- **Adapter 注册**（``get_adapter`` / ``list_engines``）：方言/DDL 生成；
- **DSN scheme 识别**（``engine_for_dsn``）：数据源连接串前缀 → 引擎名，顺序敏感
  （长前缀优先：kyuubi 在 hive 前，doris/starrocks 在 mysql 前）；
- **驱动安装提示**（``engine_driver_hint``）：缺 DBAPI 驱动时提示装哪个包。
"""

from __future__ import annotations

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
