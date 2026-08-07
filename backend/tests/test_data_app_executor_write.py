"""写侧执行器 execute_write：真实建表/写数、失败整事务回滚、回执结构。

用文件型 SQLite 验证（内存库跨连接不共享，无法在执行后另开连接核对落地结果）。
生成器的数仓方言 DDL 不在这里测——execute_write 只负责"把给定语句真正执行并如实
回执"，故用最普适的 SQL 断言其机制。
"""

from __future__ import annotations

from sqlalchemy import create_engine, text

from app.services.data_app_executor import execute_write


def _dsn(tmp_path) -> str:
    return f"sqlite:///{tmp_path / 'w.db'}"


def test_write_creates_table_and_inserts(tmp_path):
    dsn = _dsn(tmp_path)
    receipt = execute_write(
        dsn=dsn,
        statements=[
            "CREATE TABLE IF NOT EXISTS dim_customer (id INTEGER, name TEXT)",
            "INSERT INTO dim_customer VALUES (1, 'a')",
            "INSERT INTO dim_customer VALUES (2, 'b')",
        ],
    )
    assert receipt["total"] == 3
    assert receipt["executed"] == 3
    assert receipt["failed"] == 0
    assert receipt["error"] is None
    assert all(ps["ok"] for ps in receipt["per_statement"])

    # 真正落了地：另开连接核对
    with create_engine(dsn).connect() as conn:
        rows = conn.execute(text("SELECT count(*) FROM dim_customer")).scalar()
    assert rows == 2


def test_failure_rolls_back_dml(tmp_path):
    """DML 批次中途失败 → 整批回滚（事务型 backend）。

    注：DDL 在 SQLite/Hive/Doris 等引擎会隐式提交，无法回滚——那类"半成品"靠
    CREATE TABLE IF NOT EXISTS 的幂等性兜底，不靠事务。故这里用纯 DML 验证回滚。
    """
    dsn = _dsn(tmp_path)
    execute_write(dsn=dsn, statements=["CREATE TABLE t (id INTEGER PRIMARY KEY)"])

    receipt = execute_write(
        dsn=dsn,
        statements=[
            "INSERT INTO t VALUES (1)",
            "INSERT INTO t VALUES (1)",  # 主键冲突 → 失败
        ],
    )
    assert receipt["failed"] >= 1
    assert receipt["executed"] == 0
    assert receipt["error"] is not None
    assert any(ps.get("rolled_back") for ps in receipt["per_statement"])

    # 先前那条 INSERT 也被回滚：表里没有行
    with create_engine(dsn).connect() as conn:
        rows = conn.execute(text("SELECT count(*) FROM t")).scalar()
    assert rows == 0


def test_blank_statements_ignored(tmp_path):
    receipt = execute_write(
        dsn=_dsn(tmp_path),
        statements=["  ", "", "CREATE TABLE t (id INTEGER)"],
    )
    assert receipt["total"] == 1
    assert receipt["executed"] == 1


# --------------------------------------------------------------- 库级删除硬闸


def test_write_refuses_drop_database(tmp_path):
    """DROP DATABASE 一律拒绝——写侧只用于建表落数，任何情况下都不删库。"""
    import pytest

    from app.services.data_app_executor import ExecutionError

    with pytest.raises(ExecutionError, match="库级删除"):
        execute_write(dsn=_dsn(tmp_path), statements=["DROP DATABASE analytics"])


def test_write_refuses_drop_schema_and_aborts_whole_batch(tmp_path):
    """整批拒绝而非跳过那一条：批次本就是一个事务，漏执行会留下半套结构。"""
    import pytest
    from sqlalchemy import create_engine, inspect

    from app.services.data_app_executor import ExecutionError

    dsn = _dsn(tmp_path)
    with pytest.raises(ExecutionError, match="库级删除"):
        execute_write(
            dsn=dsn,
            statements=[
                "CREATE TABLE IF NOT EXISTS keep_me (id INTEGER)",
                "DROP SCHEMA IF EXISTS staging",
            ],
        )
    # 前面那条建表也没有被执行——闸在执行之前就落下了
    assert "keep_me" not in inspect(create_engine(dsn)).get_table_names()


def test_write_still_allows_drop_table_for_staging_swap(tmp_path):
    """不误伤 DROP TABLE：staging 原子切换靠它收尾，拦掉等于废掉物化。"""
    dsn = _dsn(tmp_path)
    receipt = execute_write(
        dsn=dsn,
        statements=[
            "CREATE TABLE IF NOT EXISTS dim_x__stg (id INTEGER)",
            "DROP TABLE IF EXISTS dim_x__stg",
        ],
    )
    assert receipt["executed"] == 2
    assert receipt["error"] is None
