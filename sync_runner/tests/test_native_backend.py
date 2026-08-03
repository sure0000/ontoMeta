"""native backend：sqlite→sqlite 验证分批搬运、列映射、增量水位。

不需要 MariaDB/Doris——native 建在 SQLAlchemy 反射上，一套代码对 sqlite 同样成立，故用
sqlite 覆盖逻辑；真实 MySQL/Doris 的连通留给部署侧验收（§5 M14）。
"""

from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from sync_runner.backends.native import run_native
from sync_runner.contract import WireColumn, WireEndpoint, WireJobSpec


def _seed(tmp_path):
    src = f"sqlite:///{tmp_path}/src.db"
    tgt = f"sqlite:///{tmp_path}/tgt.db"
    se, te = create_engine(src), create_engine(tgt)
    with se.begin() as c:
        c.execute(text("CREATE TABLE cust (id INTEGER, nm TEXT, dt TEXT)"))
        c.execute(
            text(
                "INSERT INTO cust VALUES "
                "(1,'a','2024-01-01'),(2,'b','2024-02-01'),(3,'c','2024-03-01')"
            )
        )
    with te.begin() as c:
        c.execute(text("CREATE TABLE dim_cust (id INTEGER, name TEXT, dt TEXT)"))
    se.dispose()
    te.dispose()
    return make_url(src), make_url(tgt)


def _spec(mode="full", pk=None):
    return WireJobSpec(
        name="j",
        source=WireEndpoint(alias="s", platform="sqlite", table="cust"),
        target=WireEndpoint(alias="t", platform="sqlite", table="dim_cust"),
        # 列映射：源 nm → 目标 name（验证映射，不是同名直搬）。
        columns=[
            WireColumn(source="id", target="id"),
            WireColumn(source="nm", target="name"),
            WireColumn(source="dt", target="dt"),
        ],
        mode=mode,
        partition_key=pk,
    )


def test_full_copies_all_and_maps_columns(tmp_path):
    src, tgt = _seed(tmp_path)
    res = run_native(_spec("full"), source_url=src, target_url=tgt, batch_size=2)
    assert (res.rows_read, res.rows_written) == (3, 3)
    with create_engine(tgt).connect() as c:
        rows = c.execute(text("SELECT id, name FROM dim_cust ORDER BY id")).fetchall()
    assert rows == [(1, "a"), (2, "b"), (3, "c")]


def test_full_rerun_replaces_not_appends(tmp_path):
    """full 事务内先删后写：重跑不累积（现状 DROP_DATA 的替代，且失败可回滚）。"""
    src, tgt = _seed(tmp_path)
    run_native(_spec("full"), source_url=src, target_url=tgt)
    res = run_native(_spec("full"), source_url=src, target_url=tgt)
    with create_engine(tgt).connect() as c:
        n = c.execute(text("SELECT count(*) FROM dim_cust")).scalar()
    assert n == 3 and res.rows_written == 3


def test_incremental_reads_watermark_from_target_and_filters_strictly_greater(tmp_path):
    """增量水位回读目标表 max(分区键)，源端 WHERE pk > 水位（严格大于，不信调用方 watermark）。"""
    src, tgt = _seed(tmp_path)
    # 目标已有上一轮的数据到 2024-02-01（回读得到的水位）。
    te = create_engine(str(tgt))
    with te.begin() as c:
        c.execute(
            text(
                "INSERT INTO dim_cust (id, name, dt) VALUES "
                "(1,'a','2024-01-01'),(2,'b','2024-02-01')"
            )
        )
    te.dispose()

    res = run_native(
        _spec("incremental", pk="dt"),
        source_url=src,
        target_url=tgt,
        watermark="1999-01-01",  # 刻意给个错的调用方水位，证明被忽略
    )
    # 只搬 dt > 2024-02-01 的一行（2024-03-01）；边界行 02-01 不重复搬。
    assert res.rows_read == 1
    assert res.watermark_before == "2024-02-01"  # 来自目标 max，不是调用方给的 1999
    assert res.watermark_after == "2024-03-01"
    with create_engine(str(tgt)).connect() as c:
        n = c.execute(text("SELECT count(*) FROM dim_cust")).scalar()
    assert n == 3  # 原有 2 行 + 新增 1 行，边界不重复


def test_incremental_empty_target_is_initial_full_load(tmp_path):
    """目标空表 → 回读水位为 None → 不加谓词，整表初始装载。"""
    src, tgt = _seed(tmp_path)
    res = run_native(_spec("incremental", pk="dt"), source_url=src, target_url=tgt)
    assert res.rows_read == 3
    assert res.watermark_before is None
    assert res.watermark_after == "2024-03-01"


def test_columns_default_to_all_when_unmapped(tmp_path):
    src, tgt = _seed(tmp_path)
    spec = WireJobSpec(
        name="j",
        source=WireEndpoint(alias="s", platform="sqlite", table="cust"),
        target=WireEndpoint(alias="t", platform="sqlite", table="dim_cust"),
        columns=[],  # 不给映射 → 源表全列同名搬（列名恰好对得上）
        mode="full",
    )
    # 源列 nm 与目标列 name 不同名：不给映射时按源列名插，dim_cust 无 nm 列 → 失败。
    # 这里换一张同名目标表验证「空映射=全列」路径本身可用。
    te = create_engine(tgt)
    with te.begin() as c:
        c.execute(text("CREATE TABLE cust (id INTEGER, nm TEXT, dt TEXT)"))
    te.dispose()
    spec.target.table = "cust"
    res = run_native(spec, source_url=src, target_url=tgt)
    assert res.rows_read == 3 and res.rows_written == 3
