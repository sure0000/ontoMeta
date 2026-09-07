"""M4 数仓方言接入：backend 识别与方言委托给 Adapter。

关键约束：数仓引擎的方言翻译**必须委托给 app/warehouse 的 Adapter**，
不能在执行器里另开一套——否则同一引擎存在两份方言逻辑，迟早分叉。
"""

from __future__ import annotations

import pytest

from app.services import data_app_executor as ex
from app.warehouse import get_adapter

# ---------- backend 识别 ----------


@pytest.mark.parametrize(
    "dsn,expected",
    [
        ("sqlite:////tmp/a.db", "sqlite"),
        ("duckdb:///x", "duckdb"),
        ("postgresql+psycopg://u@h/db", "postgres"),
        ("mysql+pymysql://u@h/db", "mysql"),
        ("hive://h:10000/dwd_erp", "hive"),
        ("kyuubi://h:10009/dwd_erp", "kyuubi"),
        ("doris://u@h:9030/ads", "doris"),
        ("starrocks://u@h:9030/ads", "starrocks"),
        ("clickhouse+http://h:8123/ads", "clickhouse"),
    ],
)
def test_backend_detection(dsn, expected):
    assert ex._backend_of(dsn) == expected


def test_mysql_protocol_dsn_still_reads_as_mysql():
    """Doris/StarRocks 走 MySQL 线协议；写成 mysql:// 时无法区分，按 mysql 处理。"""
    assert ex._backend_of("mysql+pymysql://u@doris-host:9030/ads") == "mysql"


# ---------- 方言委托 ----------


def test_hive_dialect_delegates_to_adapter():
    sql = "SELECT * FROM t WHERE d > DATE_SUB(CURDATE(), INTERVAL 7 DAY)"
    assert ex._translate_dialect(sql, "hive") == get_adapter("hive").translate_sql(sql)
    assert "current_date()" in ex._translate_dialect(sql, "hive")


def test_kyuubi_reuses_hive_adapter():
    """Kyuubi 是 Spark SQL 网关，方言等同 Hive。"""
    sql = "SELECT CURDATE()"
    assert ex._translate_dialect(sql, "kyuubi") == ex._translate_dialect(sql, "hive")


def test_sqlite_branch_unchanged():
    """本地分析引擎的既有行为不能被改动。"""
    sql = "SELECT * FROM t WHERE d > DATE_SUB(CURDATE(), INTERVAL 30 DAY)"
    assert ex._translate_dialect(sql, "sqlite") == (
        "SELECT * FROM t WHERE d > date('now','-30 day')"
    )


def test_unknown_backend_passes_through():
    sql = "SELECT CURDATE()"
    assert ex._translate_dialect(sql, "oracle") == sql


# ---------- 只读校验对数仓 SQL 同样生效 ----------


def test_read_only_guard_applies_to_warehouse_sql():
    ok, reason = ex.is_read_only("INSERT OVERWRITE TABLE dwd_erp.x SELECT 1")
    assert not ok and reason


def test_select_on_warehouse_table_passes():
    ok, _ = ex.is_read_only("SELECT customer_id FROM dim_erp.customer")
    assert ok
