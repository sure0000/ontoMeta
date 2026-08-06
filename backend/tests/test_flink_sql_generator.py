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
    ddl = render_create_table(_source(), _hive("erp_readonly"))
    assert "CREATE TABLE `customer`" in ddl
    assert "'connector' = 'jdbc'" in ddl
    # 类型映射
    assert "`customer_id` BIGINT" in ddl
    assert "`customer_name` STRING" in ddl
    assert "`amount` DECIMAL(38, 18)" in ddl
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
