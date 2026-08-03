"""native backend：内置 DB 分批搬运（M14 默认档）。

**能力边界**（不吹，见 §3.1）：按列映射 ``SELECT`` → 分批读 → 批量写，覆盖 full 与
incremental。**CDC 不做**——那要 binlog 订阅，路由到 seatunnel 档（M14 未含）。

**为什么用 SQLAlchemy 反射而不是手拼 SQL**：源/目标表两边反射出来，用 Core 的
``select().label()`` 做列映射、``table.insert()`` 批量写——标识符引用、类型绑定、分页游标
全由方言处理，一套代码同时对 sqlite（测试）/ MariaDB（源）/ Doris（目标，MySQL 线协议）成立，
不必为每种库手写引用规则。

**full 的落地语义**：先 ``DELETE`` 再插入，**同一事务**——搬到一半失败会回滚，正式表
不被清空。这已比现状的 SeaTunnel ``DROP_DATA``（先删后写、失败即丢数据，§1.2）安全。
真正的 staging + 原子切换、各引擎切换语法留给 M15，不在 native 里做。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import MetaData, Table, create_engine, delete, func, select
from sqlalchemy.engine import URL

from sync_runner.contract import WireColumn, WireJobSpec


@dataclass
class NativeResult:
    rows_read: int
    rows_written: int
    watermark_before: str | None
    watermark_after: str | None


def run_native(
    spec: WireJobSpec,
    *,
    source_url: URL,
    target_url: URL,
    watermark: str | None = None,
    batch_size: int = 1000,
) -> NativeResult:
    """把一张表从源搬到目标。返回读/写行数与水位，供回执如实反映「搬了多少」。

    ``watermark`` 参数仅为兼容保留：**增量装载不信它**，而是回读目标表的
    ``max(分区键)``（§3.3）——手动触发 / catchup=False / 补数三种场景下，调度器给的
    data_interval 与「上次成功到现在」并不等价，信它会漏数或重复。
    """
    src_engine = create_engine(source_url)
    tgt_engine = create_engine(target_url)
    try:
        src_tbl = Table(
            spec.source.table,
            MetaData(),
            autoload_with=src_engine,
            schema=spec.source.database,
        )
        tgt_tbl = Table(
            spec.target.table,
            MetaData(),
            autoload_with=tgt_engine,
            schema=spec.target.database,
        )

        # 列映射：给了就用，没给则源表全列同名搬。目标列名 = 本体属性名。
        mappings = list(spec.columns) or [
            WireColumn(source=c.name, target=c.name) for c in src_tbl.columns
        ]
        cols = [src_tbl.c[m.source].label(m.target) for m in mappings]
        stmt = select(*cols)

        # 分区键可能在源/目标两侧列名不同（被列映射改过），两侧各自解析：
        # 目标侧用于回读水位，源侧用于过滤。未在映射里出现则两侧同名。
        pk = spec.partition_key
        pk_source = pk_target = None
        if pk:
            pk_source = next((m.source for m in mappings if m.target == pk), pk)
            pk_target = next((m.target for m in mappings if m.source == pk), pk)

        watermark_before = None
        if spec.mode == "incremental" and pk:
            # 回读目标表 max(分区键) 作为水位起点（不信调用方给的 watermark）。
            with tgt_engine.connect() as wm_conn:
                watermark_before = wm_conn.execute(
                    select(func.max(tgt_tbl.c[pk_target]))
                ).scalar()
            if watermark_before is not None:
                # 严格大于：不重复搬边界行。适合单调递增主键；同值晚到行会漏
                # （如同一天多条、分区键为日期），这类应在契约上用更细的水位列。
                stmt = stmt.where(src_tbl.c[pk_source] > watermark_before)

        rows_read = rows_written = 0
        # 新高水位从旧水位起步：本次没有更新行时，水位保持不变而非回退到 None。
        wm_after = watermark_before
        with src_engine.connect() as sconn, tgt_engine.begin() as tconn:
            if spec.mode == "full":
                # 事务内先清后写：失败整体回滚，正式表不被清空（优于 DROP_DATA）。
                tconn.execute(delete(tgt_tbl))
            result = sconn.execution_options(stream_results=True).execute(stmt)
            while True:
                batch = result.fetchmany(batch_size)
                if not batch:
                    break
                dicts = [dict(row._mapping) for row in batch]
                rows_read += len(dicts)
                tconn.execute(tgt_tbl.insert(), dicts)
                rows_written += len(dicts)
                if pk_target:
                    for d in dicts:
                        v = d.get(pk_target)
                        if v is not None and (wm_after is None or v > wm_after):
                            wm_after = v

        return NativeResult(
            rows_read=rows_read,
            rows_written=rows_written,
            watermark_before=None if watermark_before is None else str(watermark_before),
            watermark_after=None if wm_after is None else str(wm_after),
        )
    finally:
        src_engine.dispose()
        tgt_engine.dispose()
