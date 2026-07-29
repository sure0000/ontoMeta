"""M2 Dialect Adapter：能力矩阵完整性、Hive DDL 快照、类型映射、能力不足即报错。

核心断言是最后一组：**本体要求的东西目标引擎表达不了时必须抛错，绝不静默降级**——
这是「完整适配层」区别于「写几个 if-else」的分界线。
"""

from __future__ import annotations

import pytest

from app.warehouse import (
    CapabilityError,
    ConstraintSupport,
    GapSeverity,
    LogicalColumn,
    LogicalConstraint,
    LogicalTable,
    get_adapter,
    list_adapters,
    list_engines,
)
from app.warehouse.registry import UnknownEngineError


def _customer_table(**overrides) -> LogicalTable:
    base = dict(
        name="customer",
        database="dim_erp",
        layer="dim",
        comment="客户",
        columns=(
            LogicalColumn("customer_id", "string", "identifier", "客户ID", nullable=False),
            LogicalColumn("customer_name", "string", "attribute", "客户名称"),
            LogicalColumn("total_amount", "decimal", "amount", "累计成交额"),
            LogicalColumn("is_vip", "boolean", "flag", "是否VIP"),
            LogicalColumn("created_at", "timestamp", "datetime", "创建时间"),
        ),
        constraints=(
            LogicalConstraint("primary_key", ("customer_id",)),
            LogicalConstraint(
                "foreign_key", ("region_id",), "dim_erp.region", ("region_id",)
            ),
        ),
        partition_key="created_at",
    )
    base.update(overrides)
    return LogicalTable(**base)


# ---------- 注册表 ----------


def test_all_engines_registered():
    assert set(list_engines()) == {
        "hive",
        "doris",
        "iceberg",
        "starrocks",
        "clickhouse",
    }


def test_unknown_engine_raises():
    with pytest.raises(UnknownEngineError):
        get_adapter("teradata")


# ---------- 能力矩阵完整性 ----------


def test_every_adapter_declares_capabilities():
    """每个 Adapter 都必须显式声明能力——即便渲染尚未实现。"""
    for adapter in list_adapters():
        caps = adapter.capabilities()
        assert caps.engine == adapter.name
        assert isinstance(caps.primary_key, ConstraintSupport)
        assert isinstance(caps.foreign_key, ConstraintSupport)
        assert caps.max_identifier_length > 0


def test_all_engines_verified_after_m8():
    """M8 后四引擎能力矩阵均已对照官方文档逐项核实——不再把「没核实」藏在注释里。"""
    verified = {a.name for a in list_adapters() if a.capabilities().verified}
    assert verified == {"hive", "doris", "iceberg", "starrocks", "clickhouse"}


def test_unverified_capabilities_yields_warning_gap():
    """能力矩阵未核实的机制仍须生效——用合成 caps 验证（真实引擎已全部核实）。"""
    from app.warehouse.capabilities import Capabilities, check_table

    caps = Capabilities(
        engine="future_engine",
        primary_key=ConstraintSupport.NONE,
        foreign_key=ConstraintSupport.NONE,
        verified=False,
    )
    gaps = check_table(_customer_table(scd_type="none", constraints=(), partition_key=None), caps)
    assert any(
        g.feature == "unverified_capabilities" and g.severity is GapSeverity.WARNING
        for g in gaps
    )


def test_every_engine_renders_without_notimplemented():
    """M8 后没有占位 Adapter——每个引擎都能渲染建表，不再抛 NotImplementedError。"""
    simple = LogicalTable(
        name="t", database="dim", layer="dim",
        columns=(LogicalColumn("id", "string", "identifier", "ID", nullable=False),),
        constraints=(LogicalConstraint("primary_key", ("id",)),),
    )
    for engine in list_engines():
        ddl = get_adapter(engine).render_create_table(simple)
        assert ddl.startswith("CREATE TABLE") or ddl.startswith("CREATE EXTERNAL TABLE")


def test_unimplemented_base_still_guards_future_engines():
    """占位基类仍保留：未来新增引擎在补齐渲染前，声明能力即可被 Gate 提前拦截。"""
    from app.warehouse.adapters.base import UnimplementedAdapter
    from app.warehouse.capabilities import Capabilities

    class _Future(UnimplementedAdapter):
        name = "future"

        def capabilities(self) -> Capabilities:
            return Capabilities(
                engine="future",
                primary_key=ConstraintSupport.NONE,
                foreign_key=ConstraintSupport.NONE,
            )

    with pytest.raises(NotImplementedError):
        _Future().render_create_table(_customer_table())


# ---------- Hive 类型映射 ----------


@pytest.mark.parametrize(
    "data_type,semantic_type,expected",
    [
        ("string", "datetime", "TIMESTAMP"),
        ("timestamp", None, "TIMESTAMP"),
        ("decimal", "amount", "DECIMAL(18,4)"),
        ("string", "amount", "DECIMAL(18,4)"),
        ("int", None, "INT"),
        ("bigint", None, "BIGINT"),
        ("double", None, "DOUBLE"),
        ("boolean", None, "BOOLEAN"),
        ("string", "flag", "BOOLEAN"),
        ("varchar", "attribute", "STRING"),
        (None, None, "STRING"),
    ],
)
def test_hive_type_mapping(data_type, semantic_type, expected):
    assert get_adapter("hive").map_type(data_type, semantic_type) == expected


# ---------- Hive DDL ----------


def test_hive_create_table_snapshot():
    ddl = get_adapter("hive").render_create_table(_customer_table())
    assert ddl == (
        "CREATE EXTERNAL TABLE IF NOT EXISTS `dim_erp`.`customer` (\n"
        "  `customer_id` STRING COMMENT '客户ID',\n"
        "  `customer_name` STRING COMMENT '客户名称',\n"
        "  `total_amount` DECIMAL(18,4) COMMENT '累计成交额',\n"
        "  `is_vip` BOOLEAN COMMENT '是否VIP'\n"
        ")\n"
        "COMMENT '客户'\n"
        "PARTITIONED BY (`created_at` TIMESTAMP COMMENT '创建时间')\n"
        "STORED AS ORC\n"
        "TBLPROPERTIES (\n"
        "  'ontometa.foreign_key.region_id'='dim_erp.region(region_id)',\n"
        "  'ontometa.layer'='dim',\n"
        "  'ontometa.primary_key'='customer_id'\n"
        ");"
    )


def test_partition_column_excluded_from_column_list():
    """Hive 语义：分区列不得出现在列清单里，否则建表直接失败。"""
    ddl = get_adapter("hive").render_create_table(_customer_table())
    body = ddl.split("PARTITIONED BY")[0]
    assert "`created_at`" not in body
    assert "PARTITIONED BY (`created_at` TIMESTAMP" in ddl


def test_comments_reach_physical_layer():
    """注释由本体反补物理层——这是本架构的关键价值点，丢了就白做。"""
    ddl = get_adapter("hive").render_create_table(_customer_table())
    for comment in ("客户", "客户ID", "客户名称", "累计成交额", "创建时间"):
        assert f"'{comment}'" in ddl


def test_hive_declares_fk_since_it_cannot_enforce():
    """Hive 没有真外键，但必须以声明式记录，而不是假装能约束。"""
    caps = get_adapter("hive").capabilities()
    assert caps.foreign_key is ConstraintSupport.DECLARATIVE
    ddl = get_adapter("hive").render_create_table(_customer_table())
    assert "'ontometa.foreign_key.region_id'='dim_erp.region(region_id)'" in ddl


def test_comment_quote_escaped():
    table = _customer_table(
        comment="客户's 维表", partition_key=None, constraints=(), columns=(
            LogicalColumn("id", "string", "identifier", "标'识"),
        )
    )
    ddl = get_adapter("hive").render_create_table(table)
    assert "客户\\'s" in ddl
    assert "标\\'识" in ddl


# ---------- 能力不足 → 报错，不静默降级 ----------


def test_scd2_on_hive_raises_not_silently_downgraded():
    """M1 允许把维度钉成 SCD2；Hive（非事务 ORC）做不到 —— 必须在渲染前报错。"""
    table = _customer_table(scd_type="scd2")
    adapter = get_adapter("hive")

    gaps = adapter.check(table)
    assert any(g.feature == "scd2" and g.is_error for g in gaps)

    with pytest.raises(CapabilityError) as exc:
        adapter.render_create_table(table)
    assert "scd2" in str(exc.value).lower()
    assert exc.value.engine == "hive"


def test_partition_key_not_in_columns_is_error():
    table = _customer_table(partition_key="nonexistent_col")
    with pytest.raises(CapabilityError):
        get_adapter("hive").render_create_table(table)


def test_identifier_length_over_limit_is_error():
    table = _customer_table(
        name="x" * 200, partition_key=None, constraints=(), columns=(
            LogicalColumn("id", "string", "identifier", "ID"),
        )
    )
    with pytest.raises(CapabilityError):
        get_adapter("hive").render_create_table(table)


def test_warning_gaps_returned_not_swallowed():
    """warning 级缺口不阻断渲染，但必须回传给调用方呈现。"""
    adapter = get_adapter("hive")
    table = _customer_table(scd_type="none")
    gaps = adapter.guard(table)  # 不抛错
    assert all(not g.is_error for g in gaps)


# ---------- ALTER ----------


def test_alter_add_column():
    adapter = get_adapter("hive")
    before = _customer_table()
    after = _customer_table(
        columns=(*before.columns, LogicalColumn("email", "string", "attribute", "邮箱"))
    )
    stmts = adapter.render_alter(before, after)
    assert any("ADD COLUMNS (`email` STRING COMMENT '邮箱')" in s for s in stmts)


def test_alter_drop_column_returns_rebuild_guidance():
    """Hive 不支持逐列 DROP；物理表是本体的投影，重建是正当手段。"""
    adapter = get_adapter("hive")
    before = _customer_table()
    after = _customer_table(columns=tuple(
        c for c in before.columns if c.name != "is_vip"
    ))
    stmts = adapter.render_alter(before, after)
    assert any("需重建" in s and "is_vip" in s for s in stmts)
    assert adapter.capabilities().supports_alter_drop_column is False


def test_alter_comment_change_emits_change_column():
    adapter = get_adapter("hive")
    before = _customer_table()
    after = _customer_table(columns=tuple(
        LogicalColumn(c.name, c.data_type, c.semantic_type, "客户全称")
        if c.name == "customer_name"
        else c
        for c in before.columns
    ))
    stmts = adapter.render_alter(before, after)
    assert any("CHANGE COLUMN `customer_name`" in s and "客户全称" in s for s in stmts)


# ---------- SQL 方言 ----------


def test_hive_translate_sql():
    adapter = get_adapter("hive")
    assert adapter.translate_sql("SELECT CURDATE()") == "SELECT current_date()"
    assert (
        adapter.translate_sql("WHERE d > DATE_SUB(CURDATE(), INTERVAL 30 DAY)")
        == "WHERE d > date_sub(current_date(), 30)"
    )


# ========== M8：四引擎补齐 ==========

# ---------- Doris ----------


@pytest.mark.parametrize(
    "data_type,semantic_type,expected",
    [
        ("date", None, "DATE"),
        ("timestamp", "datetime", "DATETIME"),  # Doris 无 TIMESTAMP 类型
        ("decimal", "amount", "DECIMAL(18,4)"),
        ("int", None, "INT"),
        ("bigint", None, "BIGINT"),
        ("boolean", "flag", "BOOLEAN"),
        ("varchar", "attribute", "VARCHAR(1024)"),  # 泛字符串落 VARCHAR（可作 Key）
    ],
)
def test_doris_type_mapping(data_type, semantic_type, expected):
    assert get_adapter("doris").map_type(data_type, semantic_type) == expected


def test_doris_create_table_snapshot():
    ddl = get_adapter("doris").render_create_table(_customer_table())
    assert ddl == (
        "CREATE TABLE IF NOT EXISTS `dim_erp`.`customer` (\n"
        '  `customer_id` VARCHAR(1024) COMMENT "客户ID",\n'
        '  `customer_name` VARCHAR(1024) COMMENT "客户名称",\n'
        '  `total_amount` DECIMAL(18,4) COMMENT "累计成交额",\n'
        '  `is_vip` BOOLEAN COMMENT "是否VIP",\n'
        '  `created_at` DATETIME COMMENT "创建时间"\n'
        ")\n"
        "UNIQUE KEY(`customer_id`)\n"
        'COMMENT "客户"\n'
        "AUTO PARTITION BY RANGE (date_trunc(`created_at`, 'day')) ()\n"
        "DISTRIBUTED BY HASH(`customer_id`) BUCKETS 10;"
    )


def test_doris_key_columns_lead():
    """Doris 表模型硬约束：Key 列必须前导。主键即使在本体里非首列也要排到最前。"""
    table = _customer_table(
        columns=(
            LogicalColumn("customer_name", "string", "attribute", "名称"),
            LogicalColumn("customer_id", "string", "identifier", "ID", nullable=False),
        ),
        partition_key=None,
        constraints=(LogicalConstraint("primary_key", ("customer_id",)),),
    )
    ddl = get_adapter("doris").render_create_table(table)
    # 列定义中 customer_id 必须先于 customer_name 出现（首次出现即列清单顺序）。
    assert ddl.index("`customer_id`") < ddl.index("`customer_name`")


def test_doris_scd2_raises_no_merge():
    """Doris 目标版本无 MERGE INTO → SCD2 契约在渲染前报错，不静默降级。"""
    with pytest.raises(CapabilityError):
        get_adapter("doris").render_create_table(_customer_table(scd_type="scd2"))


def test_doris_alter_supports_drop_column():
    adapter = get_adapter("doris")
    before = _customer_table()
    after = _customer_table(columns=tuple(c for c in before.columns if c.name != "is_vip"))
    stmts = adapter.render_alter(before, after)
    assert any("DROP COLUMN `is_vip`" in s for s in stmts)


def test_doris_translate_sql_keeps_mysql_dialect():
    # Doris 兼容 MySQL 日期函数，无需改写
    assert get_adapter("doris").translate_sql("SELECT CURDATE()") == "SELECT CURDATE()"


# ---------- StarRocks ----------


def test_starrocks_create_table_snapshot():
    ddl = get_adapter("starrocks").render_create_table(_customer_table())
    assert ddl == (
        "CREATE TABLE IF NOT EXISTS `dim_erp`.`customer` (\n"
        '  `customer_id` VARCHAR(1024) COMMENT "客户ID",\n'
        '  `customer_name` VARCHAR(1024) COMMENT "客户名称",\n'
        '  `total_amount` DECIMAL(18,4) COMMENT "累计成交额",\n'
        '  `is_vip` BOOLEAN COMMENT "是否VIP",\n'
        '  `created_at` DATETIME COMMENT "创建时间"\n'
        ")\n"
        "PRIMARY KEY(`customer_id`)\n"
        'COMMENT "客户"\n'
        "PARTITION BY date_trunc('day', `created_at`)\n"
        "DISTRIBUTED BY HASH(`customer_id`)\n"
        "PROPERTIES (\n"
        '  "foreign_key_constraints" = "(region_id) REFERENCES dim_erp.region(region_id)"\n'
        ");"
    )


def test_starrocks_declares_fk_for_optimizer():
    """StarRocks 与 Doris 的关键差异：外键可声明（供优化器 Join 改写）。"""
    caps = get_adapter("starrocks").capabilities()
    assert caps.foreign_key is ConstraintSupport.DECLARATIVE
    assert caps.primary_key_model == "primary_key"


def test_starrocks_identifier_limit_is_1024_not_64():
    """核实结论：StarRocks 表/列名上限 1024，不是 MySQL 惯常的 64。"""
    assert get_adapter("starrocks").capabilities().max_identifier_length == 1024
    long_name = "c" * 200
    table = _customer_table(
        name="t", partition_key=None, constraints=(),
        columns=(LogicalColumn(long_name, "string", "attribute", "x"),),
    )
    get_adapter("starrocks").render_create_table(table)  # 200 < 1024，不报错


def test_starrocks_scd2_raises_no_sql_merge():
    with pytest.raises(CapabilityError):
        get_adapter("starrocks").render_create_table(_customer_table(scd_type="scd2"))


# ---------- Iceberg ----------


def test_iceberg_create_table_snapshot():
    ddl = get_adapter("iceberg").render_create_table(_customer_table())
    assert ddl == (
        "CREATE TABLE IF NOT EXISTS `dim_erp`.`customer` (\n"
        "  `customer_id` STRING COMMENT '客户ID',\n"
        "  `customer_name` STRING COMMENT '客户名称',\n"
        "  `total_amount` DECIMAL(18,4) COMMENT '累计成交额',\n"
        "  `is_vip` BOOLEAN COMMENT '是否VIP',\n"
        "  `created_at` TIMESTAMP COMMENT '创建时间'\n"
        ")\n"
        "USING iceberg\n"
        "PARTITIONED BY (`created_at`)\n"
        "COMMENT '客户'\n"
        "TBLPROPERTIES (\n"
        "  'format-version'='2',\n"
        "  'ontometa.foreign_key.region_id'='dim_erp.region(region_id)',\n"
        "  'ontometa.layer'='dim',\n"
        "  'ontometa.primary_key'='customer_id'\n"
        ");"
    )


def test_iceberg_partition_column_stays_in_schema():
    """与 Hive 相反：Iceberg 分区列留在列清单里（identity 变换引用）。"""
    ddl = get_adapter("iceberg").render_create_table(_customer_table())
    body = ddl.split("USING iceberg")[0]
    assert "`created_at` TIMESTAMP" in body
    assert "PARTITIONED BY (`created_at`)" in ddl


def test_iceberg_supports_scd2_merge():
    """Iceberg 是唯一支持 SCD2 的目标引擎（原生 MERGE INTO）——不报错。"""
    assert get_adapter("iceberg").capabilities().supports_scd2_merge is True
    ddl = get_adapter("iceberg").render_create_table(_customer_table(scd_type="scd2"))
    assert "'ontometa.scd_type'='scd2'" in ddl


def test_iceberg_alter_is_native_no_rebuild():
    """Iceberg field-id 稳定，删列直接 DROP，不像 Hive 需重建。"""
    adapter = get_adapter("iceberg")
    before = _customer_table()
    after = _customer_table(columns=tuple(c for c in before.columns if c.name != "is_vip"))
    stmts = adapter.render_alter(before, after)
    assert any("DROP COLUMN `is_vip`" in s for s in stmts)
    assert not any("需重建" in s for s in stmts)


# ---------- ClickHouse ----------


def test_clickhouse_create_table_snapshot():
    ddl = get_adapter("clickhouse").render_create_table(_customer_table())
    assert ddl == (
        "CREATE TABLE IF NOT EXISTS `dim_erp`.`customer` (\n"
        "  `customer_id` String COMMENT '客户ID',\n"
        "  `customer_name` Nullable(String) COMMENT '客户名称',\n"
        "  `total_amount` Nullable(Decimal(18, 4)) COMMENT '累计成交额',\n"
        "  `is_vip` Nullable(Bool) COMMENT '是否VIP',\n"
        "  `created_at` Nullable(DateTime64(3)) COMMENT '创建时间'\n"
        ")\n"
        "ENGINE = MergeTree\n"
        "PARTITION BY toYYYYMM(`created_at`)\n"
        "ORDER BY (`customer_id`)\n"
        "COMMENT '客户';"
    )


def test_clickhouse_order_by_key_is_not_nullable():
    """ORDER BY 键列不可空；非键列按 nullable 包 Nullable(T)。"""
    ddl = get_adapter("clickhouse").render_create_table(_customer_table())
    assert "`customer_id` String COMMENT" in ddl  # 键列无 Nullable 包裹
    assert "`customer_name` Nullable(String)" in ddl


def test_clickhouse_no_bucketing_and_no_merge():
    caps = get_adapter("clickhouse").capabilities()
    assert caps.supports_bucketing is False
    assert caps.supports_scd2_merge is False
    with pytest.raises(CapabilityError):
        get_adapter("clickhouse").render_create_table(_customer_table(scd_type="scd2"))


def test_clickhouse_translate_sql_uses_today():
    adapter = get_adapter("clickhouse")
    assert adapter.translate_sql("SELECT CURDATE()") == "SELECT today()"
    assert (
        adapter.translate_sql("WHERE d > DATE_SUB(CURDATE(), INTERVAL 7 DAY)")
        == "WHERE d > today() - 7"
    )


# ---------- 引号下沉（遗留2）----------


def test_quote_identifier_delegated_to_adapter():
    """引号规则住在 Adapter：生成器只委托 quote_identifier，不自己判引擎。"""
    for engine in list_engines():
        assert get_adapter(engine).quote_identifier("col") == "`col`"
