"""warehouse.registry 引擎知识单一真源:scheme 识别 / 驱动提示 / kyuubi 别名。

统一查询网关重构后,DSN scheme → 引擎、引擎 → 驱动提示、kyuubi → hive Adapter
全部收敛到注册表一处,查询侧不再自维护第二套映射。这些测试钉住行为等价。
"""

from __future__ import annotations

from app.warehouse import (
    UnknownEngineError,
    engine_driver_hint,
    engine_for_dsn,
    get_adapter,
)


def test_engine_for_dsn_scheme_mapping():
    assert engine_for_dsn("hive://h:10000/db") == "hive"
    assert engine_for_dsn("kyuubi://h:10009/db") == "kyuubi"
    assert engine_for_dsn("starrocks://fe:9030/db") == "starrocks"
    assert engine_for_dsn("doris://fe:9030/db") == "doris"
    assert engine_for_dsn("clickhouse://h:8123/db") == "clickhouse"
    assert engine_for_dsn("postgresql://u:p@h:5432/db") == "postgres"
    assert engine_for_dsn("postgres://u:p@h:5432/db") == "postgres"
    assert engine_for_dsn("mysql+pymysql://u:p@h:3306/db") == "mysql"


def test_engine_for_dsn_order_sensitive():
    """长前缀优先:kyuubi 在 hive 前,warehouse 引擎在 mysql 前(行为与原 executor 一致)。"""
    assert engine_for_dsn("kyuubi://h:10009/db").startswith("kyuubi")
    assert engine_for_dsn("mysql+pymysql://u:p@h:3306/db") == "mysql"
    # 显式 doris/starrocks scheme 识别为自身,不被 mysql 抢走
    assert engine_for_dsn("doris+pymysql://fe:9030/db") == "doris"
    assert engine_for_dsn("starrocks+pymysql://fe:9030/db") == "starrocks"


def test_engine_for_dsn_local_engines_return_none():
    """sqlite/duckdb 无 DialectAdapter,executor 就地处理——不注册为引擎。"""
    assert engine_for_dsn("sqlite:///f.db") is None
    assert engine_for_dsn("duckdb:///f.db") is None


def test_engine_for_dsn_unknown_prefix():
    assert engine_for_dsn("unknownscheme://x") is None


def test_kyuubi_adapter_is_hive():
    """Kyuubi 是 Spark SQL 网关,方言等同 Hive——复用同一实例。"""
    assert get_adapter("kyuubi") is get_adapter("hive")


def test_unknown_engine_raises():
    try:
        get_adapter("not-an-engine")
        assert False, "应抛 UnknownEngineError"
    except UnknownEngineError:
        pass


def test_engine_driver_hints():
    assert "pymysql" in engine_driver_hint("starrocks")
    assert "psycopg2" in engine_driver_hint("postgres")
    assert "pyhive" in engine_driver_hint("hive")
    assert "pyhive" in engine_driver_hint("kyuubi")
    assert "duckdb" in engine_driver_hint("duckdb")
    assert engine_driver_hint("not-an-engine") is None
