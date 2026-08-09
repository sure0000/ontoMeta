"""P1-1：Flink SQL 生成器（计算侧）单元测试。

钉住：JDBC connector 逐表声明、常规类型映射、batch/streaming 切换、凭据只走占位符、
FROM 引用 Flink 裸表名、幂等。
"""

from __future__ import annotations

import re

import pytest

from app.services.flink_sql_generator import (
    FlinkEndpoint,
    generate_flink_sql,
    render_create_table,
)
from app.warehouse import LogicalColumn, LogicalTable


def _source() -> LogicalTable:
    return LogicalTable(
        name="customer",
        database="erp_ods",
        layer="dwd",
        columns=(
            LogicalColumn("customer_id", "bigint", "id", "客户ID"),
            LogicalColumn("customer_name", "string", "name", "客户名"),
            LogicalColumn("amount", "decimal", "amount", "金额"),
            LogicalColumn("created_at", "datetime", "datetime", "创建时间"),
        ),
    )


def _target() -> LogicalTable:
    return LogicalTable(
        name="dim_customer",
        database="dim_erp",
        layer="dim",
        columns=(
            LogicalColumn("customer_id", "bigint", "id", "客户ID"),
            LogicalColumn("customer_name", "string", "name", "客户名"),
        ),
    )


def _hive(alias: str) -> FlinkEndpoint:
    return FlinkEndpoint(alias=alias, platform="hive")


# ---------- CREATE TABLE ----------


def test_create_table_uses_jdbc_connector_and_maps_types():
    # is_target=True：目标表的类型镜像该引擎 Adapter 的产物；源表则按物理类型（见下一个用例）
    ddl = render_create_table(_source(), _hive("erp_readonly"), is_target=True)
    assert "CREATE TABLE `customer`" in ddl
    assert "'connector' = 'jdbc'" in ddl
    # 类型映射
    assert "`customer_id` BIGINT" in ddl
    assert "`customer_name` STRING" in ddl
    # 目标/源端是已注册引擎时，类型以该引擎 Adapter 的产物为准（数仓里那一列**实际**
    # 就是这个类型）——Flink 另算一套的话，JDBC sink 会报 column types do not match。
    # hive 的 decimal+amount 是 DECIMAL(18,4)，不是 Flink 的通用默认 DECIMAL(38,18)。
    assert "`amount` DECIMAL(18, 4)" in ddl
    assert "`created_at` TIMESTAMP(3)" in ddl
    # Hive JDBC 驱动
    assert "org.apache.hive.jdbc.HiveDriver" in ddl
    # 注释反补
    assert "COMMENT '客户名'" in ddl


def test_create_table_carries_no_credentials_only_placeholders():
    ddl = render_create_table(_source(), _hive("erp_readonly"))
    # 别名派生的占位符
    assert "${ERP_READONLY_URL}" in ddl
    assert "${ERP_READONLY_USER}" in ddl
    assert "${ERP_READONLY_PASSWORD}" in ddl
    # 任何 url/username/password 值都必须是占位符
    for value in re.findall(r"'(?:url|username|password)' = '([^']*)'", ddl):
        assert value.startswith("${")


def test_doris_target_uses_doris_connector():
    target = LogicalTable(
        name="ads_gmv", database="ads_erp", layer="ads",
        columns=(LogicalColumn("gmv", "decimal", "amount", "GMV"),),
    )
    ddl = render_create_table(target, FlinkEndpoint("dw_doris", "doris"))
    assert "'connector' = 'doris'" in ddl
    assert "'fenodes' = '${DW_DORIS_FENODES}'" in ddl
    assert "'table.identifier' = 'ads_erp.ads_gmv'" in ddl


def test_streaming_source_gets_watermark():
    ddl = render_create_table(
        _source(), _hive("erp"),
        watermark=("created_at", "`created_at` - INTERVAL '5' SECOND"),
    )
    assert "WATERMARK FOR `created_at` AS" in ddl


# ---------- 完整脚本 ----------


def test_batch_script_sets_batch_mode_and_wraps_insert():
    sql = generate_flink_sql(
        source_table=_source(),
        target_table=_target(),
        source=_hive("erp_readonly"),
        target=_hive("dw"),
        select_body="SELECT `customer_id`, `customer_name` FROM `customer` WHERE `customer_id` IS NOT NULL",
        execution_mode="batch",
    )
    assert "SET 'execution.runtime-mode' = 'batch';" in sql
    # 源与目标都声明
    assert "CREATE TABLE `customer`" in sql
    assert "CREATE TABLE `dim_customer`" in sql
    # INSERT 用目标裸表名，FROM 用源裸表名
    assert "INSERT INTO `dim_customer`" in sql
    assert "FROM `customer`" in sql
    # 收尾分号
    assert sql.rstrip().endswith(";")


def test_streaming_script_sets_streaming_mode():
    sql = generate_flink_sql(
        source_table=_source(),
        target_table=_target(),
        source=_hive("erp"),
        target=_hive("dw"),
        select_body="SELECT `customer_id`, `customer_name` FROM `customer`",
        execution_mode="streaming",
        source_watermark=("created_at", "`created_at` - INTERVAL '5' SECOND"),
    )
    assert "SET 'execution.runtime-mode' = 'streaming';" in sql
    assert "WATERMARK FOR `created_at`" in sql


def test_batch_ignores_watermark():
    sql = generate_flink_sql(
        source_table=_source(),
        target_table=_target(),
        source=_hive("erp"),
        target=_hive("dw"),
        select_body="SELECT `customer_id` FROM `customer`",
        execution_mode="batch",
        source_watermark=("created_at", "x"),
    )
    assert "WATERMARK" not in sql


def test_generation_is_idempotent():
    kwargs = dict(
        source_table=_source(),
        target_table=_target(),
        source=_hive("erp"),
        target=_hive("dw"),
        select_body="SELECT `customer_id` FROM `customer`",
        execution_mode="batch",
    )
    assert generate_flink_sql(**kwargs) == generate_flink_sql(**kwargs)


def test_select_body_trailing_semicolon_not_doubled():
    sql = generate_flink_sql(
        source_table=_source(),
        target_table=_target(),
        source=_hive("erp"),
        target=_hive("dw"),
        select_body="SELECT `customer_id` FROM `customer`;",
        execution_mode="batch",
    )
    assert ";;" not in sql


def test_source_table_uses_physical_types_not_adapter():
    """源端按**物理类型**声明：JDBC 驱动返回什么类型由源库决定，与数仓怎么建表无关。

    拿目标引擎的 Adapter 来算源端，会得到运行期
    ``ClassCastException: Integer cannot be cast to String``——MySQL 的 TINYINT
    经 JDBC 返回 Integer，而 Adapter 会把它判成文本列。
    """
    from app.services.flink_sql_generator import source_flink_type, target_flink_type
    from app.warehouse import LogicalColumn

    tinyint = LogicalColumn("docstatus", "TINYINT(4)", "category")
    assert source_flink_type(tinyint) == "INT"          # 驱动返回 Integer
    assert target_flink_type(tinyint, "postgres") == "STRING"  # 数仓里是文本列
    # 两者不同 → 生成 SELECT 时必须 CAST（见 build_flink_etl_input）


def test_flink_type_mirrors_the_target_engine_adapter():
    """目标端的列类型以 Dialect Adapter 的产物为准——数仓里那一列实际就是这个类型。

    Flink 另算一套的话，JDBC sink 直接报 "Column types of query result and sink do not
    match"。踩过一次：stat_date 在 postgres 是 DATE，Flink 按语义算成了 TIMESTAMP(3)。
    """
    from app.services.flink_sql_generator import _flink_type
    from app.warehouse import LogicalColumn, get_adapter

    pg = get_adapter("postgres")
    for data_type, semantic in [
        ("date", "datetime"), ("decimal", "amount"), ("VARCHAR(140)", "attribute"),
        ("DATETIME(6)", "datetime"), ("TEXT", "flag"), ("TINYINT(4)", "category"),
    ]:
        col = LogicalColumn("c", data_type, semantic)
        flink = _flink_type(col, "postgres")
        native = pg.map_type(data_type, semantic).lower()
        # 两边说的是同一种东西（名字不同但同类）
        same = (
            (flink == "DATE" and native.startswith("date"))
            or (flink == "TIMESTAMP(3)" and native.startswith("timestamp"))
            or (flink == "BOOLEAN" and native.startswith("bool"))
            or (flink.startswith("DECIMAL") and native.startswith(("decimal", "numeric")))
            or (flink == "STRING" and native in ("text", "varchar"))
        )
        assert same, f"{data_type}/{semantic}: flink={flink} 与 postgres={native} 对不上"


def test_flink_type_falls_back_to_physical_for_foreign_sources():
    """源库（mariadb 等）没有 Adapter 可问，按物理类型映射——但要先去参数。

    此前精确查表，``VARCHAR(140)`` / ``DATETIME(6)`` 一个都命中不了，每一列都退成 STRING。
    """
    from app.services.flink_sql_generator import _flink_type
    from app.warehouse import LogicalColumn

    assert _flink_type(LogicalColumn("c", "VARCHAR(140)"), "mariadb") == "STRING"
    assert _flink_type(LogicalColumn("c", "INTEGER(11)"), "mariadb") == "INT"
    assert _flink_type(LogicalColumn("c", "BIGINT(20)"), "mariadb") == "BIGINT"
    assert _flink_type(LogicalColumn("c", "DATETIME(6)"), "mariadb") == "TIMESTAMP(3)"
    assert _flink_type(LogicalColumn("c", "DECIMAL(21, 9)"), "mariadb") == "DECIMAL(38, 18)"


def test_jdbc_table_name_depends_on_platform():
    """MySQL 的「库」在 URL 里，Postgres 的 `dim` 是库内 schema——同一个词两层含义。

    此前一律写 `库.表`：MySQL 侧被解析成 `库.库.表`，真跑一次才报
    `Table '库.库.表' doesn't exist`，生成期完全看不出来。
    """
    from app.services.flink_sql_generator import _jdbc_table_name

    # URL 末段已是库 → table-name 只能写裸表名
    assert _jdbc_table_name("mysql", "erp_db.tabBrand") == "tabBrand"
    assert _jdbc_table_name("mariadb", "erp_db.tabBrand") == "tabBrand"
    # postgres 的 dim 是 schema，必须保留
    assert _jdbc_table_name("postgres", "dim.brand") == "dim.brand"
    assert _jdbc_table_name("hive", "dim.brand") == "dim.brand"
    # 本来就没库前缀的原样返回
    assert _jdbc_table_name("mysql", "tabBrand") == "tabBrand"
