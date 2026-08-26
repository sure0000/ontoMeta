"""P1-1：Flink SQL 生成器（计算侧）单元测试。

钉住：JDBC connector 逐表声明、常规类型映射、batch/streaming 切换、凭据只走占位符、
FROM 引用 Flink 裸表名、幂等。
"""

from __future__ import annotations

import re

import pytest

from app.services.flink_sql_generator import (
    FlinkEndpoint,
    build_identity_select,
    generate_flink_sql,
    generate_move_sql,
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


def test_source_type_ignores_semantic_flag():
    """语义类型不参与**源端**声明：flag 的物理类型是 TINYINT，驱动给的是 Integer。

    ``is_group`` 语义是 flag、物理是 ``TINYINT(4)``。把语义搬到源端，源表就声明成
    BOOLEAN，作业提交得进集群、运行期才炸
    ``ClassCastException: Integer cannot be cast to Boolean``——错误不在提交回执里，
    要翻 Flink 作业的 exceptions 才看得见。语义只决定这列在数仓里建成什么。
    """
    from app.services.flink_sql_generator import source_flink_type, target_flink_type
    from app.warehouse import LogicalColumn

    flag = LogicalColumn("is_group", "TINYINT(4)", "flag")
    assert source_flink_type(flag) == "INT"                     # 源端＝物理
    assert target_flink_type(flag, "doris") == "BOOLEAN"        # 目标端＝语义经 Adapter

    # 物理本来就是布尔的列，源端照旧是 BOOLEAN（去掉语义不等于一律不认布尔）
    real_bool = LogicalColumn("is_active", "BOOLEAN", "flag")
    assert source_flink_type(real_bool) == "BOOLEAN"


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


# ---------- 搬运（sync/materialize）：恒等投影 ----------


def test_identity_select_maps_and_aliases_columns():
    """搬运的恒等体：每个目标列 = 源列 AS 目标列，FROM 引用源裸表名。"""
    body = build_identity_select(_source(), _target(), "hive")
    assert body.startswith("SELECT")
    assert "`customer_id` AS `customer_id`" in body
    assert "`customer_name` AS `customer_name`" in body
    assert body.rstrip().endswith("FROM `customer`")


def test_identity_select_casts_on_type_mismatch():
    """源物理类型与目标引擎类型不同 → 显式 CAST（否则 Flink 提交期拒绝）。"""
    # 源列 docstatus 物理是 TINYINT(4)（驱动返回 Integer→INT），目标语义 category→STRING
    source = LogicalTable(
        name="doc", database="erp_ods", layer="ods",
        columns=(LogicalColumn("docstatus", "TINYINT(4)", "category"),),
    )
    target = LogicalTable(
        name="dim_doc", database="dim_erp", layer="dim",
        columns=(LogicalColumn("docstatus", "TINYINT(4)", "category"),),
    )
    body = build_identity_select(source, target, "postgres")
    assert "CAST(`docstatus` AS STRING) AS `docstatus`" in body


def test_identity_select_honors_column_map():
    """目标列名 ≠ 源物理列名时，按 column_map 取源列名。"""
    source = LogicalTable(
        name="src_c", database=None, layer="ods",
        columns=(LogicalColumn("cust_nm", "string", "name"),),
    )
    target = LogicalTable(
        name="dim_customer", database="dim_erp", layer="dim",
        columns=(LogicalColumn("customer_name", "string", "name"),),
    )
    body = build_identity_select(
        source, target, "hive", column_map={"customer_name": "cust_nm"}
    )
    assert "`cust_nm` AS `customer_name`" in body


def test_generate_move_sql_full_is_batch_insert():
    """全量搬运 = batch 模式 + 恒等 INSERT，源/目标都声明，凭据只走占位符。"""
    sql = generate_move_sql(
        source_table=_source(),
        target_table=_target(),
        source=_hive("erp_readonly"),
        target=_hive("dw"),
        target_engine="hive",
        mode="full",
    )
    assert "SET 'execution.runtime-mode' = 'batch';" in sql
    assert "CREATE TABLE `customer`" in sql
    assert "CREATE TABLE `dim_customer`" in sql
    assert "INSERT INTO `dim_customer`" in sql
    assert "FROM `customer`" in sql
    assert "${ERP_READONLY_URL}" in sql
    # 无真实值凭据
    for value in re.findall(r"'(?:url|username|password)' = '([^']*)'", sql):
        assert value.startswith("${")


def test_generate_move_sql_is_idempotent():
    kwargs = dict(
        source_table=_source(), target_table=_target(),
        source=_hive("erp"), target=_hive("dw"), target_engine="hive", mode="full",
    )
    assert generate_move_sql(**kwargs) == generate_move_sql(**kwargs)


def test_move_sql_source_ddl_and_projection_agree_on_flag_columns():
    """源表 DDL 与投影必须同口径：flag 列源端声明 INT、投影里才 CAST 成 BOOLEAN。

    两条路径各算各的时，产物会自相矛盾——源表写 ``is_group BOOLEAN``、投影却写
    ``CAST(is_group AS BOOLEAN)``。SQL 提交得进集群，运行期 MySQL 驱动对 tinyint(4)
    返回 Integer，在 TaskManager 里炸 ``Integer cannot be cast to Boolean``，而 Airflow
    只看得到一句 "Failed to wait job finish"。
    """
    flag_src = LogicalTable(
        name="src_customer_group", database=None, layer="ods",
        columns=(LogicalColumn("is_group", "TINYINT(4)", "flag", "是否为分组节点"),),
    )
    flag_tgt = LogicalTable(
        name="ods_customer_group", database="ods", layer="ods",
        columns=(LogicalColumn("is_group", "TINYINT(4)", "flag", "是否为分组节点"),),
    )
    sql = generate_move_sql(
        source_table=flag_src, target_table=flag_tgt,
        source=FlinkEndpoint(alias="erp", platform="mariadb"),
        target=FlinkEndpoint(alias="dw", platform="doris"),
        target_engine="doris", mode="full",
    )
    source_ddl, _, rest = sql.partition("-- 目标表")
    assert "`is_group` INT" in source_ddl        # 源端＝驱动会返回的类型
    assert "`is_group` BOOLEAN" in rest          # 目标端＝数仓里的类型
    assert "CAST(`is_group` AS BOOLEAN)" in rest  # 差一档就得 CAST


def _pg(alias: str) -> FlinkEndpoint:
    return FlinkEndpoint(alias=alias, platform="postgres")


def test_generate_move_sql_cdc_is_streaming_with_cdc_connector_and_checkpoint():
    """CDC 搬运 = streaming + postgres-cdc 源连接器 + checkpoint SET。"""
    sql = generate_move_sql(
        source_table=_source(),
        target_table=_target(),
        source=_pg("erp_src"),
        target=_pg("dw"),
        target_engine="postgres",
        mode="cdc",
        source_physical="erp_db.public.customer",
        checkpoint_dir="file:///tmp/ckpt",
    )
    assert "SET 'execution.runtime-mode' = 'streaming';" in sql
    # CDC 源连接器（拆开的 hostname/port，不是 JDBC url）
    assert "'connector' = 'postgres-cdc'" in sql
    assert "'hostname' = '${ERP_SRC_HOSTNAME}'" in sql
    assert "'port' = '${ERP_SRC_PORT}'" in sql
    assert "'database-name' = '${ERP_SRC_DATABASE}'" in sql
    assert "'schema-name' = 'public'" in sql
    assert "'table-name' = 'customer'" in sql
    # checkpoint 走本地文件（standalone/YARN 通用）
    assert "SET 'state.backend' = 'filesystem';" in sql
    assert "SET 'state.checkpoints.dir' = 'file:///tmp/ckpt';" in sql


def test_generate_move_sql_incremental_is_bounded_jdbc():
    """incremental uses a bounded JDBC predicate and persisted watermark."""
    sql = generate_move_sql(
        source_table=_source(), target_table=_target(),
        source=FlinkEndpoint("erp_src", "mysql"), target=_hive("dw"),
        target_engine="hive", mode="incremental",
        source_physical="erp_db.customer",
        incremental_column="created_at", watermark="2026-01-01 00:00:00",
    )
    assert "'connector' = 'mysql-cdc'" not in sql
    assert "SET 'execution.runtime-mode' = 'batch';" in sql
    assert "WHERE `created_at` >= '2026-01-01 00:00:00'" in sql


def test_generate_move_sql_cdc_requires_checkpoint_dir():
    """CDC 不给 checkpoint_dir → 报错（否则重启从头重搬，违背增量语义）。"""
    with pytest.raises(ValueError, match="checkpoint"):
        generate_move_sql(
            source_table=_source(), target_table=_target(),
            source=_pg("erp_src"), target=_pg("dw"),
            target_engine="postgres", mode="cdc",
        )


def test_generate_move_sql_cdc_rejects_source_without_cdc_connector():
    """源平台无 CDC 连接器（如 hive）→ 报错，不静默退回全量。"""
    with pytest.raises(ValueError, match="CDC"):
        generate_move_sql(
            source_table=_source(), target_table=_target(),
            source=_hive("erp"), target=_hive("dw"),
            target_engine="hive", mode="cdc", checkpoint_dir="file:///tmp/ckpt",
        )


def test_generate_move_sql_full_has_no_checkpoint_or_cdc():
    """全量是批作业：无 checkpoint SET、源用 JDBC 而非 CDC。"""
    sql = generate_move_sql(
        source_table=_source(), target_table=_target(),
        source=_pg("erp_src"), target=_pg("dw"),
        target_engine="postgres", mode="full",
    )
    assert "streaming" not in sql
    assert "state.backend" not in sql
    assert "-cdc" not in sql
    assert "'connector' = 'jdbc'" in sql


def test_generate_move_sql_rejects_unknown_mode():
    with pytest.raises(ValueError):
        generate_move_sql(
            source_table=_source(), target_table=_target(),
            source=_hive("erp"), target=_hive("dw"),
            target_engine="hive", mode="bogus",
        )


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
