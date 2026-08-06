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


@dataclass(frozen=True)
class FlinkEndpoint:
    """Flink 里一张表对应的物理端点。``alias`` 是唯一的凭据线索（占位符由它派生），
    ``platform`` 决定 connector 与驱动。本对象不含主机/账号/密码。"""

    alias: str
    platform: str


def _flink_type(col: LogicalColumn) -> str:
    """列类型 → Flink SQL 类型（常规默认映射，未知回退 STRING）。"""
    base = (col.data_type or "string").lower().strip()
    return _TYPE_MAP.get(base, "STRING")


def _placeholder(alias: str, field: str) -> str:
    """凭据占位符，与搬运通道同构（不变量 5：凭据不进 Spec）。"""
    token = _alias_token(alias)
    return f"${{{token}_{field.upper()}}}"


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
        "table-name": physical,
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
) -> str:
    """渲染一张表的 Flink SQL ``CREATE TABLE ... WITH (...)``。

    Args:
        table: 逻辑表（列、注释）；``table.name`` 作 Flink 逻辑表名
        endpoint: 物理端点（别名 + 平台）
        physical_name: 数仓真实库表名（JDBC table-name），缺省用 ``table.qualified_name``
        watermark: streaming 源表的 ``(列名, 策略)``
    """
    col_lines = [
        f"  `{c.name}` {_flink_type(c)}"
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
        render_create_table(target_table, target, physical_name=target_physical),
        "",
        "-- 计算并写入",
        f"INSERT INTO `{target_table.name}`",
        select_body.rstrip().rstrip(";") + ";",
    ]
    return "\n".join(parts)
