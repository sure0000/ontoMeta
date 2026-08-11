"""Flink SQL 血缘解析（L1/A1）——从一条 Flink SQL 脚本里抽出源表与目标表。

**为什么做这个**：Airflow DataHub 插件按任务的 inlets/outlets 自动上报血缘，
而 Flink SQL 任务此前「暂不注入 inlets/outlets」（airflow_dag_builder 遗留）。
本模块把一条 ``generate_flink_sql`` / ``generate_move_sql`` 产出的脚本解析成
「源表物理名列表 + 目标表物理名」，供调用方转成 Dataset URN 注入 DAG 的 inlets/outlets。

**解析范围刻意收窄**：只认本仓库生成器产出的两种形态——
1. ``CREATE TABLE 逻辑名 (...) WITH ('table-name'='库.表', ...)`` 声明临时表；
2. ``INSERT INTO 目标逻辑名 SELECT ... FROM 源逻辑名 [JOIN ...]`` 计算体。

不做通用 SQL 解析（不引入解析器依赖）。识别不到源/目标时**返回空、不抛错**——
血缘是增强，解析失败不该阻断任务执行（执行本身不依赖血缘）。

**逻辑表名 → 物理表名**：脚本里 SELECT 的 FROM 引用的是 Flink 逻辑表名
（``CREATE TABLE`` 声明名），物理名藏在 WITH 的 ``table-name`` 里。故先扫全部
CREATE TABLE 建立「逻辑名 → 物理名」映射，再在 INSERT 体里按 FROM/JOIN 找逻辑名，
反查物理名。

**产物字段**：
- ``source_tables``：物理名列表（``库.表`` 或裸表名），按 FROM/JOIN 出现顺序
- ``target_table``：物理名（INSERT INTO 的目标表）

**与 URN 的关系**：物理名是中间产物；转 URN 由调用方用
``datahub.build_dataset_urn(platform, name, fabric)`` 做（平台/环境属部署事实，
本模块不感知）。
"""

from __future__ import annotations

import re

#: CREATE TABLE 语句：捕获逻辑表名。
#: ``CREATE TABLE `name` (... ) WITH (...);`` 或 ``CREATE TABLE name (...)``
_CREATE_TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?`?([A-Za-z0-9_]+)`?\s*\(",
    re.IGNORECASE,
)

#: WITH 子句里的 table-name 键值：``'table-name' = '库.表'``（或 table.identifier）
_TABLE_NAME_RE = re.compile(
    r"'(?:table-name|table\.identifier)'\s*=\s*'([^']+)'",
    re.IGNORECASE,
)

#: INSERT INTO：捕获目标逻辑表名（``INSERT INTO `t` ...``）
_INSERT_INTO_RE = re.compile(
    r"INSERT\s+INTO\s+`?([A-Za-z0-9_]+)`?",
    re.IGNORECASE,
)

#: FROM / JOIN 子句：捕获源逻辑表名（``FROM `s```、``JOIN `s2` ON ...``）。
#: 刻意不匹配子查询（FROM (SELECT ...)）——子查询的源表属于嵌套逻辑，本模块不展开。
_FROM_JOIN_RE = re.compile(
    r"\b(?:FROM|JOIN)\s+`?([A-Za-z0-9_]+)`?",
    re.IGNORECASE,
)


class FlinkLineage:
    """一次解析的结果。``source_tables`` / ``target_table`` 均为物理名。"""

    __slots__ = ("source_tables", "target_table")

    def __init__(self, source_tables: list[str], target_table: str | None) -> None:
        self.source_tables = list(dict.fromkeys(source_tables))  # 保序去重
        self.target_table = target_table

    def to_dict(self) -> dict:
        return {
            "source_tables": self.source_tables,
            "target_table": self.target_table,
        }


def parse_flink_sql_lineage(sql: str) -> FlinkLineage:
    """从一条 Flink SQL 脚本解析血缘。识别不到时返回空字段，不抛错。

    Args:
        sql: ``generate_flink_sql`` / ``generate_move_sql`` 的产物。

    Returns:
        FlinkLineage：source_tables（物理名列表）/ target_table（物理名或 None）。
    """
    if not sql:
        return FlinkLineage([], None)

    # 1. 扫全部 CREATE TABLE，建「逻辑名 → 物理名」映射。
    logical_to_physical: dict[str, str] = {}
    for match in _CREATE_TABLE_RE.finditer(sql):
        logical = match.group(1)
        # 该 CREATE TABLE 的 WITH 段：从声明起点往后找 table-name（就近匹配）。
        tail = sql[match.end() :]
        # 取到下一个 CREATE TABLE 前的一段，避免跨语句误配。
        next_create = _CREATE_TABLE_RE.search(tail)
        segment = tail[: next_create.start()] if next_create else tail
        tm = _TABLE_NAME_RE.search(segment)
        if tm:
            logical_to_physical[logical] = tm.group(1)

    # 2. INSERT INTO 找目标逻辑表。
    insert_match = _INSERT_INTO_RE.search(sql)
    target_table: str | None = None
    if insert_match:
        target_logical = insert_match.group(1)
        target_table = logical_to_physical.get(target_logical, target_logical)

    # 3. FROM / JOIN 找源逻辑表（只在 INSERT 体里找——CREATE TABLE 声明里也可能有
    #    "FROM" 字样，但那不是数据源）。取 INSERT 之后的部分。
    insert_pos = insert_match.end() if insert_match else 0
    body = sql[insert_pos:]
    source_tables: list[str] = []
    for match in _FROM_JOIN_RE.finditer(body):
        logical = match.group(1)
        if logical in logical_to_physical:
            source_tables.append(logical_to_physical[logical])
        else:
            # 逻辑名未声明（可能是裸表名直用），原样保留。
            source_tables.append(logical)

    return FlinkLineage(source_tables, target_table)


def task_lineage_urns(
    *,
    sql: str | None = None,
    source_tables: list[str] | tuple[str, ...] = (),
    target_table: str | None = None,
    source_platform: str = "hive",
    target_platform: str = "hive",
    fabric: str = "PROD",
) -> tuple[tuple[str, ...], str]:
    """把一次任务的源/目标物理名转成 inlets/outlets URN（L1）。

    **优先用调用方显式给的物理名**（executor 已算好的 ``source_physical`` /
    ``target_physical`` 最准）；没给时用 A1 从 SQL 解析（:func:`parse_flink_sql_lineage`）。

    Args:
        sql: Flink SQL 脚本（A1 回退解析用）。
        source_tables: 源物理名列表（``库.表`` 或裸表名）。
        target_table: 目标物理名。
        source_platform: 源表所在平台（决定 URN 的 dataPlatform）。
        target_platform: 目标表所在平台。
        fabric: DataHub 环境标（PROD/DEV/…），部署事实，由调用方从设置取。

    Returns:
        ``(source_urns, target_urn)``：可直接填进 ``FlinkSqlTask``。
    """
    from app.connectors.datahub import build_dataset_urn

    srcs = list(source_tables)
    tgt = target_table
    if sql and (not srcs or not tgt):
        lg = parse_flink_sql_lineage(sql)
        if not srcs:
            srcs = lg.source_tables
        if not tgt:
            tgt = lg.target_table
    source_urns = tuple(
        build_dataset_urn(source_platform, s, fabric) for s in srcs
    )
    target_urn = (
        build_dataset_urn(target_platform, tgt, fabric) if tgt else ""
    )
    return source_urns, target_urn
