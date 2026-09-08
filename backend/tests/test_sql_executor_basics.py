"""只读 SQL 执行器：安全校验、真实 SQLite 执行、物理映射。

执行器是全平台共用的取数底座（Agent 取数、字段画像、物化 DDL 都走它），
与已经下线的自研数据应用无关——那一层的用例随模块一并删除。
"""

from __future__ import annotations

import sqlite3

import pytest

from app.services.data_app_executor import (
    ExecutionError,
    execute_sql,
    is_read_only,
)

# --------------------------------------------------------------- executor unit


@pytest.mark.parametrize(
    "sql,ok",
    [
        ("SELECT * FROM orders", True),
        ("WITH t AS (SELECT 1 AS a) SELECT a FROM t", True),
        ("select channel, sum(amount) from orders group by channel", True),
        ("INSERT INTO orders VALUES (1)", False),
        ("UPDATE orders SET amount = 0", False),
        ("DELETE FROM orders", False),
        ("DROP TABLE orders", False),
        ("SELECT 1; SELECT 2", False),
        ("SELECT * FROM orders; DROP TABLE orders", False),
        ("", False),
    ],
)
def test_is_read_only(sql, ok):
    result, _reason = is_read_only(sql)
    assert result is ok


def test_execute_sql_sqlite(tmp_path):
    db_path = tmp_path / "phys.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE orders (channel TEXT, amount REAL);
        INSERT INTO orders VALUES ('A', 100), ('A', 50), ('B', 30);
        """
    )
    conn.commit()
    conn.close()

    dsn = f"sqlite:///{db_path}"
    columns, rows = execute_sql(
        dsn=dsn,
        sql="SELECT channel, SUM(amount) AS sum_amount FROM orders GROUP BY channel",
        limit=100,
    )
    keys = {c["key"] for c in columns}
    assert "channel" in keys and "sum_amount" in keys
    by_channel = {r["channel"]: r["sum_amount"] for r in rows}
    assert by_channel["A"] == 150
    assert by_channel["B"] == 30


def test_execute_sql_enforces_limit(tmp_path):
    db_path = tmp_path / "phys2.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        "CREATE TABLE t (x INTEGER);"
        + "".join(f"INSERT INTO t VALUES ({i});" for i in range(10))
    )
    conn.commit()
    conn.close()
    _cols, rows = execute_sql(dsn=f"sqlite:///{db_path}", sql="SELECT x FROM t", limit=3)
    assert len(rows) == 3


def test_execute_sql_rejects_write(tmp_path):
    db_path = tmp_path / "phys3.db"
    sqlite3.connect(db_path).close()
    with pytest.raises(ExecutionError):
        execute_sql(dsn=f"sqlite:///{db_path}", sql="DROP TABLE t", limit=1)


def test_execute_sql_mapping(tmp_path):
    db_path = tmp_path / "phys4.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        "CREATE TABLE physical_orders (chan TEXT, amt REAL);"
        "INSERT INTO physical_orders VALUES ('A', 10);"
    )
    conn.commit()
    conn.close()
    # 本体名 orders/channel/amount → 物理 physical_orders/chan/amt
    columns, rows = execute_sql(
        dsn=f"sqlite:///{db_path}",
        sql="SELECT channel, amount FROM orders",
        limit=10,
        mapping={
            "tables": {"orders": "physical_orders"},
            "columns": {"channel": "chan", "amount": "amt"},
        },
    )
    assert rows and rows[0]["chan"] == "A"
