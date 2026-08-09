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
        "postgres",
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
    """M8 后五个 OLAP 引擎能力矩阵均已对照官方文档逐项核实——不再把「没核实」藏在注释里。

    postgres（后补）能力矩阵按 PG16 文档写但尚未在真实实例逐项核实，故 verified=False——
    该机制正是要让「没核实」机器可见（产生 warning 缺口），不因新增引擎而失效。
    """
    verified = {a.name for a in list_adapters() if a.capabilities().verified}
    assert verified == {"hive", "doris", "iceberg", "starrocks", "clickhouse"}
    # postgres 已注册但故意未核实
    assert "postgres" in set(list_engines())
    assert not get_adapter("postgres").capabilities().verified


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
        # 建表语句在不在即可——不能断言它打头：postgres 要先 CREATE SCHEMA，
        # 分层在 postgres 上是 schema 而非 database，不预建整批 DDL 全报错。
        assert "CREATE TABLE" in ddl or "CREATE EXTERNAL TABLE" in ddl


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
    """引号规则住在 Adapter：生成器只委托 quote_identifier，不自己判引擎。

    多数引擎用反引号，ANSI 双引号引擎（postgres）覆写本方法——这正是引号必须住在
    Adapter 而非生成器的原因（生成器若自己判 `if engine==...` 就漏掉了这种差异）。
    """
    backtick = {"hive", "doris", "iceberg", "starrocks", "clickhouse"}
    for engine in list_engines():
        expected = '"col"' if engine == "postgres" else "`col`"
        assert get_adapter(engine).quote_identifier("col") == expected, engine
    # 反引号引擎集合未意外增减
    assert {e for e in list_engines() if e != "postgres"} == backtick


# ---------- Postgres Adapter ----------


@pytest.mark.parametrize(
    "data_type,semantic_type,expected",
    [
        ("string", "datetime", "TIMESTAMP"),
        ("timestamp", None, "TIMESTAMP"),
        ("date", None, "DATE"),
        ("decimal", "amount", "NUMERIC(18,4)"),
        ("string", "amount", "NUMERIC(18,4)"),
        ("int", None, "INTEGER"),
        ("bigint", None, "BIGINT"),
        ("double", None, "DOUBLE PRECISION"),
        ("boolean", None, "BOOLEAN"),
        ("string", "flag", "BOOLEAN"),
        (None, None, "TEXT"),
    ],
)
def test_postgres_type_mapping(data_type, semantic_type, expected):
    assert get_adapter("postgres").map_type(data_type, semantic_type) == expected


def test_postgres_ddl_uses_ansi_dialect():
    """postgres 建表：双引号标识符、真主键、NUMERIC/TIMESTAMP、无反引号/无分桶。"""
    ddl = get_adapter("postgres").render_create_table(_customer_table())
    assert 'CREATE TABLE IF NOT EXISTS "dim_erp"."customer"' in ddl
    assert '"customer_id" TEXT NOT NULL' in ddl  # identifier→TEXT，非空主键列
    assert '"total_amount" NUMERIC(18,4)' in ddl
    assert '"created_at" TIMESTAMP' in ddl
    assert 'PRIMARY KEY ("customer_id")' in ddl
    # 标准 SQL：无反引号、无 OLAP 分桶/表模型
    assert "`" not in ddl
    assert "DISTRIBUTED BY" not in ddl
    assert "STORED AS" not in ddl


def test_postgres_ddl_renders_foreign_key():
    """外键仍是真 FOREIGN KEY ... REFERENCES（postgres 能强制），但**分两段**下发。

    内联进 CREATE TABLE 时被引用表必须先存在，于是「只物化本体的一部分」和「本体里有环」
    都建不出来（ERP 本体的血缘本来就是环）。故建表语句里不该再有 FOREIGN KEY，
    约束改由 render_foreign_keys 在建表之后补。
    """
    adapter = get_adapter("postgres")
    ddl = adapter.render_create_table(_customer_table())
    assert "FOREIGN KEY" not in ddl, "外键不该再内联进建表语句"

    stmts = adapter.render_foreign_keys(_customer_table())
    joined = "\n".join(stmts)
    assert 'FOREIGN KEY ("region_id") REFERENCES "dim_erp"."region" ("region_id")' in joined
    assert "ALTER TABLE \"dim_erp\".\"customer\" ADD CONSTRAINT" in joined


def test_postgres_foreign_key_is_idempotent():
    """物化会重跑：约束语句必须能重复执行，否则第二次直接 42710（约束已存在）整批红。"""
    stmts = get_adapter("postgres").render_foreign_keys(_customer_table())
    assert stmts and all("IF NOT EXISTS" in s and "pg_constraint" in s for s in stmts)
    # 约束名要稳定，否则每跑一次多一条同义约束
    assert get_adapter("postgres").render_foreign_keys(_customer_table()) == stmts


def test_declarative_engines_emit_no_deferred_foreign_keys():
    """声明式外键的引擎（hive 写进 TBLPROPERTIES）不产第二段，行为不变。"""
    assert get_adapter("hive").render_foreign_keys(_customer_table()) == []


def test_postgres_table_and_column_comments():
    """postgres 注释走独立 COMMENT ON 语句（不能内联进 CREATE）。"""
    ddl = get_adapter("postgres").render_create_table(_customer_table())
    assert "COMMENT ON TABLE \"dim_erp\".\"customer\" IS '客户';" in ddl
    assert (
        "COMMENT ON COLUMN \"dim_erp\".\"customer\".\"customer_name\" IS '客户名称';"
        in ddl
    )


def test_postgres_no_partition_declared():
    """PoC 不建原生子分区：分区键仅作普通列（仍出现在列清单），无 PARTITION BY 子句。

    能力矩阵 supports_partition=True——postgres 能承载分区键（作普通列），声明 False 会让
    任何带分区键的表 guard() 报 error 而无法物化，那是错的；差别只在不建原生分区子表。
    """
    caps = get_adapter("postgres").capabilities()
    assert caps.supports_partition is True
    assert caps.supports_bucketing is False
    ddl = get_adapter("postgres").render_create_table(_customer_table())
    # 分区键 created_at 作为普通列存在，但没有 PARTITION BY 子句
    assert '"created_at"' in ddl
    assert "PARTITION" not in ddl


def test_postgres_staging_and_swap_use_postgres_syntax():
    """只换引号是不够的：postgres 的建表复制与改名语法都与基类默认不同。

    真实跑一次物化才暴露的——``CREATE TABLE x LIKE y``（MySQL/Hive 写法）postgres 直接
    报 "syntax error at or near LIKE"，整个 create_tables 任务红掉、下游全部 upstream_failed。
    此前 M15 的 staging 只有 golden 断言、从未接到 DAG 运行时。
    """
    adapter = get_adapter("postgres")
    table = _customer_table()
    stg = adapter.render_create_staging(table, "run-1")
    swap = adapter.render_swap(table, "run-1")
    assert stg.startswith("CREATE TABLE IF NOT EXISTS")
    assert "(LIKE " in stg and "INCLUDING ALL" in stg
    assert '"' in stg and "`" not in stg
    assert all("`" not in s for s in swap)
    renames = [s for s in swap if "RENAME TO" in s]
    assert len(renames) == 2
    # RENAME TO 只接受裸名：新名不得带库前缀，否则 postgres 语法报错
    for stmt in renames:
        new_name = stmt.split("RENAME TO", 1)[1].strip().rstrip(";")
        assert "." not in new_name, f"新名不该带库前缀：{new_name}"


def test_postgres_alter_add_drop_modify():
    """postgres 支持逐列增/删/改类型。"""
    before = _customer_table()
    after = _customer_table(
        partition_key=None,  # 删掉 created_at 列，分区键随之清空（否则指向不存在的列）
        columns=(
            LogicalColumn("customer_id", "string", "identifier", "客户ID", nullable=False),
            LogicalColumn("customer_name", "string", "attribute", "客户名称"),
            # total_amount 从 decimal 改成纯 int（无 amount 语义干扰）→ NUMERIC→INTEGER
            LogicalColumn("total_amount", "int", None, "累计成交额"),
            LogicalColumn("email", "string", "attribute", "邮箱"),  # 新增
            # 删除 is_vip / created_at
        ),
    )
    stmts = get_adapter("postgres").render_alter(before, after)
    joined = "\n".join(stmts)
    assert 'ADD COLUMN "email" TEXT' in joined
    assert 'DROP COLUMN "is_vip"' in joined
    assert 'ALTER COLUMN "total_amount" TYPE' in joined


def test_postgres_creates_schema_before_table():
    """postgres 的「分层」是 schema，必须先建 schema 再建表。

    没有这一句时，物化到一个新目标库会整批报 `schema "dim" does not exist`——
    DDL 里 `"dim"."account"` 的前半段在 postgres 语义下是 schema，不是 database，
    而它不会随建表自动出现。
    """
    table = LogicalTable(
        name="account", database="dim", layer="dim",
        columns=(LogicalColumn("id", "string", "identifier", "ID", nullable=False),),
        constraints=(LogicalConstraint("primary_key", ("id",)),),
    )
    ddl = get_adapter("postgres").render_create_table(table)
    assert 'CREATE SCHEMA IF NOT EXISTS "dim";' in ddl
    assert ddl.index("CREATE SCHEMA") < ddl.index("CREATE TABLE"), "建 schema 必须在建表之前"


def test_postgres_without_database_emits_no_schema_statement():
    """没有分层（database 为空）时不该凭空造一个 schema。"""
    table = LogicalTable(
        name="account", database=None, layer="dim",
        columns=(LogicalColumn("id", "string", "identifier", "ID", nullable=False),),
    )
    ddl = get_adapter("postgres").render_create_table(table)
    assert "CREATE SCHEMA" not in ddl


# ---------- 装载语句方言 ----------


def test_hive_family_keeps_insert_overwrite():
    """Hive 家族的写法不变——改动只针对没有 INSERT OVERWRITE 的标准 SQL 引擎。"""
    for engine in ("hive", "doris", "starrocks", "iceberg"):
        adapter = get_adapter(engine)
        full = adapter.render_load("dim.customer", "SELECT 1", overwrite=True)
        inc = adapter.render_load("dim.customer", "SELECT 1", overwrite=False)
        assert full.startswith("INSERT OVERWRITE TABLE dim.customer"), engine
        assert inc.startswith("INSERT INTO TABLE dim.customer"), engine


def test_postgres_load_uses_truncate_insert():
    """postgres 见到 INSERT OVERWRITE 直接语法报错，INSERT INTO 后也不能跟 TABLE。"""
    adapter = get_adapter("postgres")
    full = adapter.render_load("dim.customer", "SELECT 1", overwrite=True)
    assert "INSERT OVERWRITE" not in full
    assert full.startswith("TRUNCATE TABLE dim.customer;")
    assert "INSERT INTO dim.customer" in full
    inc = adapter.render_load("dim.customer", "SELECT 1", overwrite=False)
    assert inc.startswith("INSERT INTO dim.customer")
    assert "TRUNCATE" not in inc
    assert "INSERT INTO TABLE" not in inc


def test_clickhouse_load_uses_truncate_insert():
    adapter = get_adapter("clickhouse")
    full = adapter.render_load("dim.customer", "SELECT 1", overwrite=True)
    assert full.startswith("TRUNCATE TABLE dim.customer;")
    assert "INSERT OVERWRITE" not in full


def test_map_type_strips_parameters_from_physical_types():
    """带参数的原样类型要能命中：真实源给的是 INTEGER(11) / DECIMAL(21, 9)。

    此前精确查表一个都命中不了，**整数列、金额列全被判成文本**落进数仓——数值语义
    就此丢失，下游聚合要么报错要么按字符串比大小。日期类靠子串判断侥幸躲过，
    数值类没这个运气。
    """
    for engine, integer, decimal in [
        ("postgres", "INTEGER", "NUMERIC(18,4)"),
        ("hive", "INT", "DECIMAL(18,4)"),
        ("doris", "INT", "DECIMAL(18,4)"),
        ("starrocks", "INT", "DECIMAL(18,4)"),
        ("iceberg", "INT", "DECIMAL(18,4)"),
    ]:
        adapter = get_adapter(engine)
        assert adapter.map_type("INTEGER(11)", None) == integer, engine
        assert adapter.map_type("DECIMAL(21, 9)", "amount") == decimal, engine
        # 无参数的写法行为不变
        assert adapter.map_type("integer", None) == integer, engine
