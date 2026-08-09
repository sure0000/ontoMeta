"""native backend：内置 DB 分批搬运（M14 默认档）。

**能力边界**（不吹，见 §3.1）：按列映射 ``SELECT`` → 分批读 → 批量写，覆盖 full 与
incremental。**CDC 不做**——那要 binlog 订阅，路由到 seatunnel 档（M14 未含）。

**为什么用 SQLAlchemy 反射而不是手拼 SQL**：源/目标表两边反射出来，用 Core 的
``select().label()`` 做列映射、``table.insert()`` 批量写——标识符引用、类型绑定、分页游标
全由方言处理，一套代码同时对 sqlite（测试）/ MariaDB（源）/ Doris（目标，MySQL 线协议）成立，
不必为每种库手写引用规则。

**full 的落地语义**：先 ``DELETE`` 再插入，**同一事务**。但别把安全性押在这个事务上——
Doris/Hive 这类目标没有通用的多语句回滚，DELETE 一旦发出就作数了。真正兜底的是上游：
DAG 把全量作业的目标改写成 staging 表，搬完再由 Dialect Adapter 的切换语句换到正式表
（M15，见 ``services/airflow_dag_builder.py::_plan_staging``），所以这里的 DELETE 清的是
**staging 表**——它可能残留上一次失败运行的数据，必须清。切换语法各引擎不同，落在
Dialect Adapter（与建表 DDL 同处），不进 runner。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import MetaData, Table, create_engine, delete, func, select
from sqlalchemy.engine import URL

from sync_runner.contract import WireColumn, WireJobSpec


_TRUE_TOKENS = frozenset({"1", "true", "t", "yes", "y"})
_FALSE_TOKENS = frozenset({"0", "false", "f", "no", "n", ""})


class CoercionError(RuntimeError):
    """源值无法安全转成目标列类型。带列名与原值——只说「类型错」找不到是哪一列。"""


def _to_bool(column: str, value):
    """源值 → 布尔。**只认能无损判定的**，其余报错而不是猜。

    为什么需要它：目标列类型由本体的**语义类型**决定（``flag`` → BOOLEAN），源列的物理
    类型却可能是 TEXT（Frappe 的 ``_user_tags`` 存 ``["a"]``）。这是既定设计——语义优先于
    物理类型，转换是搬运该做的事。此前搬运一点转换都不做，于是建出来的表装不下自己的
    源数据，每次都挂在「Not a boolean value」上，报错还指向数据而不是那条语义判断。

    猜不出来的值不静默塞 True/False：那会把一列错误的语义判断变成一列错误的数据，
    而且再也看不出来。报错让人回去把该属性的语义类型改对。
    """
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):  # MySQL 的 TINYINT(1) 读出来是 0/1
        return bool(value)
    token = str(value).strip().lower()
    if token in _TRUE_TOKENS:
        return True
    if token in _FALSE_TOKENS:
        return False
    raise CoercionError(
        f"列 {column} 的目标类型是布尔，但源值 {value!r} 判不出真假。"
        "多半是该属性的语义类型被推断成了 flag（而源里存的是文本）——"
        "回本体把它改成 attribute，或在源侧规范该列取值。"
    )


def _coercers(tgt_tbl: Table) -> dict:
    """目标表里需要转换的列 → 转换函数。目前只有布尔一种，够用即加。"""
    out = {}
    for col in tgt_tbl.columns:
        try:
            python_type = col.type.python_type
        except NotImplementedError:  # 方言特有类型（如 JSONB）没有 python_type
            continue
        if python_type is bool:
            out[col.name] = _to_bool
    return out


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

        coercers = _coercers(tgt_tbl)
        rows_read = rows_written = 0
        # 新高水位从旧水位起步：本次没有更新行时，水位保持不变而非回退到 None。
        wm_after = watermark_before
        with src_engine.connect() as sconn, tgt_engine.begin() as tconn:
            if spec.mode == "full":
                # 清的是 staging 表（目标已由 DAG 改写），可能残留上次失败运行的数据。
                # 放在事务里是为了支持事务的目标能整体回滚；不支持的靠上游 staging 兜底。
                tconn.execute(delete(tgt_tbl))
            result = sconn.execution_options(stream_results=True).execute(stmt)
            while True:
                batch = result.fetchmany(batch_size)
                if not batch:
                    break
                dicts = [dict(row._mapping) for row in batch]
                rows_read += len(dicts)
                # 源列类型 ≠ 目标列类型时按目标列转换（目标类型由本体语义决定，见 _to_bool）。
                for column, coerce in coercers.items():
                    for row_dict in dicts:
                        if column in row_dict:
                            row_dict[column] = coerce(column, row_dict[column])
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
