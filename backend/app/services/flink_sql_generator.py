"""Flink SQL 生成器（计算侧）——transform 清洗 / metric 聚合。

与 ``app/warehouse/jobs/flink.py``（搬运 pipeline JSON）**分开**：
- flink.py 产 source→transform→sink 的搬运 pipeline（Flink CDC 3.x 语义），用于数据同步；
- 本模块产 Flink SQL 脚本（CREATE TABLE + INSERT），用于计算任务（清洗/聚合）。

两者形态不同、语义不同，不复用。

**设计决策（2026-08-06）**：
1. source/sink 用 **JDBC connector**（逐表声明列与连接参数），不依赖 Flink Hive catalog 部署；
2. 列类型按常规默认映射到 Flink SQL 类型（decimal → DECIMAL(38,18) 等）；
3. metric 允许 streaming（实时聚合需 watermark，由调用方在参数里显式给）。

**职责边界**：本模块把「源表 + 目标表 + 一段 SELECT 体」组装成完整 Flink SQL 脚本
（SET 执行模式 + CREATE TABLE source/sink + INSERT）。**SELECT 体由调用方（executor）
生成**，因为业务逻辑（清洗规则、聚合口径）在 executor 那边最清楚；但调用方生成 SELECT 时
**FROM 必须引用 ``source_table.name``**（Flink 声明的裸表名），不能带库前缀——Flink 逐表声明的
是临时表，表名空间与 Hive 的 ``库.表`` 不同。

**幂等**：同输入反复生成逐字节一致。凭据不进产物（只用 ``${ALIAS_FIELD}`` 占位符，不变量 5）。
"""

from __future__ import annotations

import re

from dataclasses import dataclass

from app.warehouse import LogicalColumn, LogicalTable
from app.warehouse.jobs.base import _alias_token

# Hive 语义的粗类型 → Flink SQL 类型（常规默认）
_TYPE_MAP = {
    "string": "STRING",
    "varchar": "STRING",
    "text": "STRING",
    "int": "INT",
    "integer": "INT",
    # MySQL 的 TINYINT/SMALLINT 经 JDBC 返回 Integer——声明成 STRING 会在运行期
    # ClassCastException（源端按物理类型声明，见 source_flink_type）。
    "tinyint": "INT",
    "smallint": "INT",
    "bigint": "BIGINT",
    "long": "BIGINT",
    "decimal": "DECIMAL(38, 18)",
    "numeric": "DECIMAL(38, 18)",
    "double": "DOUBLE",
    "float": "FLOAT",
    "boolean": "BOOLEAN",
    "bool": "BOOLEAN",
    "date": "DATE",
    "datetime": "TIMESTAMP(3)",
    "timestamp": "TIMESTAMP(3)",
}

# 引擎平台 → Flink connector 类型。未列出者兜底 jdbc。
_CONNECTORS = {
    "hive": "jdbc",  # 经 HiveServer2 的 JDBC 读写
    "doris": "doris",  # Doris 专用 connector
    "starrocks": "starrocks",  # StarRocks 专用 connector
    "clickhouse": "jdbc",
    "mysql": "jdbc",
    "mariadb": "jdbc",
    "postgres": "jdbc",
    "postgresql": "jdbc",
}

# JDBC 平台 → 驱动类。仅 jdbc connector 需要。
_JDBC_DRIVERS = {
    "hive": "org.apache.hive.jdbc.HiveDriver",
    "mysql": "com.mysql.cj.jdbc.Driver",
    "mariadb": "org.mariadb.jdbc.Driver",
    "postgres": "org.postgresql.Driver",
    "postgresql": "org.postgresql.Driver",
    "clickhouse": "com.clickhouse.jdbc.ClickHouseDriver",
}


def quote_identifier(name: str) -> str:
    """Flink SQL 的标识符引用规则：反引号。

    **与目标数仓的方言无关**——同一条 Flink 语句写进 postgres，标识符仍按 Flink 的规则
    引用，不是 postgres 的双引号。故引用规则住在本模块（Flink 方言的所在地），
    调用方不得拿 Dialect Adapter 的 ``quote_identifier`` 来引 Flink 语句。
    """
    return f"`{name}`"


@dataclass(frozen=True)
class FlinkEndpoint:
    """Flink 里一张表对应的物理端点。``alias`` 是唯一的凭据线索（占位符由它派生），
    ``platform`` 决定 connector 与驱动。本对象不含主机/账号/密码。"""

    alias: str
    platform: str


#: 引擎原生类型（Adapter.map_type 的产物）→ Flink 类型。按**前缀**匹配，第一条命中为准；
#: 顺序有意义（timestamp 要在 int 之前、bigint 要在 int 之前）。
_ENGINE_TO_FLINK: tuple[tuple[str, str], ...] = (
    ("timestamp", "TIMESTAMP(3)"),
    ("datetime", "TIMESTAMP(3)"),
    ("date", "DATE"),
    ("time", "TIMESTAMP(3)"),
    ("bool", "BOOLEAN"),
    ("bigint", "BIGINT"),
    ("smallint", "INT"),
    ("integer", "INT"),
    ("int", "INT"),
    ("double", "DOUBLE"),
    ("float", "FLOAT"),
    ("real", "DOUBLE"),
)


def _from_engine_type(native: str) -> str:
    """引擎原生列类型 → Flink 类型。decimal/numeric 保精度，其余按前缀表。"""
    lowered = (native or "").strip().lower()
    if lowered.startswith(("decimal", "numeric")):
        params = lowered[lowered.find("(") : ] if "(" in lowered else "(38, 18)"
        return "DECIMAL" + params.replace(" ", "").replace(",", ", ")
    for prefix, flink in _ENGINE_TO_FLINK:
        if lowered.startswith(prefix):
            return flink
    return "STRING"


def source_flink_type(col: LogicalColumn) -> str:
    """**源端**列类型：JDBC 驱动按源库的物理类型返回值，故只看物理类型。

    不能拿目标引擎的 Adapter 来算：那答的是「这列在数仓里该建成什么」，
    而源端要的是「驱动会返回什么 Java 类型」。差一档就是运行期
    ``ClassCastException: Integer cannot be cast to String``。
    """
    return _flink_type(col, None)


def target_flink_type(col: LogicalColumn, engine: str) -> str:
    """**目标端**列类型：镜像该引擎 Adapter 的产物（表就是照它建的）。"""
    return _flink_type(col, engine)


def _flink_type(col: LogicalColumn, platform: str | None = None) -> str:
    """列类型 → Flink SQL 类型。

    **目标端以 Dialect Adapter 的产物为准**：数仓里那一列到底是什么类型，是
    ``adapter.map_type`` 说了算的（表就是照它建的）。Flink 侧另算一套，只要有一档
    对不上，JDBC sink 就报 "Column types of query result and sink do not match"
    ——踩过一次：stat_date 在数仓是 DATE，Flink 按语义算成了 TIMESTAMP(3)。

    源端（platform 不是已注册的数仓引擎，如 mariadb）没有 Adapter 可问，按物理类型
    映射；物理类型要先去参数（``VARCHAR(140)`` / ``DATETIME(6)`` 精确查表命中不了，
    此前每一列都退成了 STRING）。
    """
    if platform:
        try:
            from app.warehouse import get_adapter

            return _from_engine_type(
                get_adapter(platform).map_type(col.data_type, col.semantic_type)
            )
        except Exception:  # noqa: BLE001 — 不是已注册引擎（源库多半如此）→ 按物理类型
            pass
    raw = (col.data_type or "string").lower().strip()
    base = re.split(r"[(\s]", raw, maxsplit=1)[0]
    st = (col.semantic_type or "").lower().strip()
    if st == "date" or base == "date":
        return "DATE"
    if st in {"datetime", "time"} or "time" in base or "date" in base:
        return "TIMESTAMP(3)"
    if st == "amount" or base in {"decimal", "numeric", "money"}:
        return "DECIMAL(38, 18)"
    if st == "flag" or base in {"bool", "boolean"}:
        return "BOOLEAN"
    return _TYPE_MAP.get(base, "STRING")


def _placeholder(alias: str, field: str) -> str:
    """凭据占位符，与搬运通道同构（不变量 5：凭据不进 Spec）。"""
    token = _alias_token(alias)
    return f"${{{token}_{field.upper()}}}"


#: URL 里已经带了库名的平台（``jdbc:mysql://host:3306/<库>``）。
#: 这些平台的「库」就是 URL 的那一段，``table-name`` 再带一次会被解析成 ``库.库.表``。
_DATABASE_IN_URL = frozenset({"mysql", "mariadb"})


def _jdbc_table_name(platform: str, physical: str) -> str:
    """JDBC ``table-name`` 该写 ``表`` 还是 ``库.表``——两类平台答案不同。

    - MySQL/MariaDB：URL 末段就是库，故这里只能写**裸表名**；带上库前缀会得到
      ``Table '库.库.表' doesn't exist``（真跑一次才会看见，生成期看不出来）。
    - PostgreSQL：URL 末段是**数据库**，而 ``dim`` 是库内的 **schema**，
      必须写成 ``schema.表``，否则找不到表。

    同一个词「database」在两边指的不是一层东西，这条差异只能按平台判。
    """
    if platform in _DATABASE_IN_URL and "." in physical:
        return physical.rsplit(".", 1)[-1]
    return physical


def _connector_props(
    table: LogicalTable, endpoint: FlinkEndpoint, physical_name: str | None = None
) -> dict[str, str]:
    """按平台产 WITH (...) 的连接属性。凭据一律占位符。

    ``physical_name`` 是数仓里的真实库表名（``库.表``），用作 JDBC ``table-name`` /
    Doris ``table.identifier``。与 Flink 逻辑表名（``table.name``，CREATE TABLE 用）分开：
    transform 里源实体与目标表常同名，Flink 逻辑名靠前缀区开，而物理名各指各的库。
    不给则回退 ``table.qualified_name``。
    """
    platform = endpoint.platform.lower()
    connector = _CONNECTORS.get(platform, "jdbc")
    alias = endpoint.alias
    physical = physical_name or table.qualified_name

    if connector == "doris":
        return {
            "connector": "doris",
            "fenodes": _placeholder(alias, "FENODES"),
            "table.identifier": physical,
            "username": _placeholder(alias, "USER"),
            "password": _placeholder(alias, "PASSWORD"),
        }
    if connector == "starrocks":
        db_name = physical.rsplit(".", 1)[0] if "." in physical else (table.database or "")
        tbl_name = physical.rsplit(".", 1)[-1]
        return {
            "connector": "starrocks",
            "jdbc-url": _placeholder(alias, "JDBC_URL"),
            "load-url": _placeholder(alias, "LOAD_URL"),
            "database-name": db_name,
            "table-name": tbl_name,
            "username": _placeholder(alias, "USER"),
            "password": _placeholder(alias, "PASSWORD"),
        }
    # 默认 JDBC
    props = {
        "connector": "jdbc",
        "url": _placeholder(alias, "URL"),
        "table-name": _jdbc_table_name(platform, physical),
        "username": _placeholder(alias, "USER"),
        "password": _placeholder(alias, "PASSWORD"),
    }
    driver = _JDBC_DRIVERS.get(platform)
    if driver:
        props["driver"] = driver
    return props


def render_create_table(
    table: LogicalTable,
    endpoint: FlinkEndpoint,
    *,
    physical_name: str | None = None,
    watermark: tuple[str, str] | None = None,
    is_target: bool = False,
) -> str:
    """渲染一张表的 Flink SQL ``CREATE TABLE ... WITH (...)``。

    Args:
        table: 逻辑表（列、注释）；``table.name`` 作 Flink 逻辑表名
        endpoint: 物理端点（别名 + 平台）
        physical_name: 数仓真实库表名（JDBC table-name），缺省用 ``table.qualified_name``
        watermark: streaming 源表的 ``(列名, 策略)``
    """
    col_lines = [
        f"  `{c.name}` {_flink_type(c, endpoint.platform if is_target else None)}"
        + (f" COMMENT '{c.comment}'" if c.comment else "")
        for c in table.columns
    ]
    if watermark:
        col_lines.append(f"  WATERMARK FOR `{watermark[0]}` AS {watermark[1]}")
    cols = ",\n".join(col_lines)

    props = _connector_props(table, endpoint, physical_name)
    props_str = ",\n  ".join(f"'{k}' = '{v}'" for k, v in props.items())
    return f"CREATE TABLE `{table.name}` (\n{cols}\n) WITH (\n  {props_str}\n);"


def generate_flink_sql(
    *,
    source_table: LogicalTable,
    target_table: LogicalTable,
    source: FlinkEndpoint,
    target: FlinkEndpoint,
    select_body: str,
    execution_mode: str = "batch",
    source_physical: str | None = None,
    target_physical: str | None = None,
    source_watermark: tuple[str, str] | None = None,
) -> str:
    """把「源表 + 目标表 + SELECT 体」组装成完整 Flink SQL 计算作业。

    Args:
        source_table: 源表（transform 的 ODS / metric 的 dwd·dws），``name`` 作 Flink 逻辑表名
        target_table: 目标表（transform 的 dwd·dim / metric 的 ads）
        source: 源端点（别名 + 平台）
        target: 目标端点
        select_body: 一段 SELECT 语句，**FROM 必须引用 ``source_table.name``**（Flink 逻辑表名），
            清洗/聚合逻辑（WHERE/GROUP BY/表达式）都在其中，由调用方生成
        execution_mode: ``batch`` 或 ``streaming``
        source_physical: 源表在数仓的真实库表名（JDBC table-name），缺省用 qualified_name
        target_physical: 目标表真实库表名，同上
        source_watermark: streaming 下源表的 ``(列名, 策略)``；batch 忽略

    Returns:
        完整 Flink SQL 脚本（幂等）
    """
    mode = "streaming" if execution_mode == "streaming" else "batch"
    watermark = source_watermark if mode == "streaming" else None

    parts = [
        f"SET 'execution.runtime-mode' = '{mode}';",
        "",
        "-- 源表",
        render_create_table(
            source_table, source, physical_name=source_physical, watermark=watermark
        ),
        "",
        "-- 目标表",
        render_create_table(
            target_table, target, physical_name=target_physical, is_target=True
        ),
        "",
        "-- 计算并写入",
        f"INSERT INTO `{target_table.name}`",
        select_body.rstrip().rstrip(";") + ";",
    ]
    return "\n".join(parts)
