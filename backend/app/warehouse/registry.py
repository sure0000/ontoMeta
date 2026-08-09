"""Dialect Adapter 注册表。

生成器只通过这里拿 Adapter——不得直接 import 具体引擎实现，否则引擎逻辑会渗出去。
"""

from __future__ import annotations

from app.warehouse.adapters.base import DialectAdapter
from app.warehouse.adapters.clickhouse import ClickHouseAdapter
from app.warehouse.adapters.doris import DorisAdapter
from app.warehouse.adapters.hive import HiveAdapter
from app.warehouse.adapters.iceberg import IcebergAdapter
from app.warehouse.adapters.postgres import PostgresAdapter
from app.warehouse.adapters.starrocks import StarRocksAdapter

# Hive 是权威写入路径（单一写入路径原则），其余由其派生。
DEFAULT_ENGINE = "hive"

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


class UnknownEngineError(KeyError):
    def __init__(self, engine: str):
        super().__init__(
            f"未知引擎 {engine!r}，可选：{', '.join(sorted(_ADAPTERS))}"
        )


def get_adapter(engine: str) -> DialectAdapter:
    try:
        return _ADAPTERS[(engine or "").lower()]
    except KeyError:
        raise UnknownEngineError(engine) from None


def list_adapters() -> list[DialectAdapter]:
    return [_ADAPTERS[name] for name in sorted(_ADAPTERS)]


def list_engines() -> list[str]:
    return sorted(_ADAPTERS)
