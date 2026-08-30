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

# 源平台 → Flink CDC 源连接器。CDC 作业的 source 用它（不吃 JDBC url，要 hostname/port
# 拆开）；未列出的平台不支持 CDC，由调用方（generate_move_sql）拦下。
_CDC_CONNECTORS = {
    "mysql": "mysql-cdc",
    "mariadb": "mysql-cdc",  # MariaDB 走 MySQL 协议与 binlog
    "postgres": "postgres-cdc",
    "postgresql": "postgres-cdc",
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
    ``platform`` 决定 connector 与驱动。本对象不含主机/账号/密码。

    ``benodes``：Doris 目标端是否显式下发 BE 地址。**只有设置页真配了才置 True**——
    置了就多发一个 ``${别名_BENODES}`` 占位符，而 SqlRunner 对缺失的环境变量是报错的，
    没配却发＝运行期报「缺少凭据环境变量」。"""

    alias: str
    platform: str
    benodes: bool = False


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

    **语义类型在这里也不算数**（``use_semantic=False``）：``is_group`` 的语义是 flag、
    物理是 ``TINYINT(4)``，MySQL 驱动对 tinyint(4) 返回的是 Integer，源表声明成 BOOLEAN
    就是运行期 ``Integer cannot be cast to Boolean``。语义只决定这列在**数仓**里建成什么，
    到目标端由 Adapter 说了算，两端不同就由 :func:`build_identity_select` CAST。
    """
    return _flink_type(col, None, use_semantic=False)


def target_flink_type(col: LogicalColumn, engine: str) -> str:
    """**目标端**列类型：镜像该引擎 Adapter 的产物（表就是照它建的）。"""
    return _flink_type(col, engine)


def _flink_type(
    col: LogicalColumn, platform: str | None = None, *, use_semantic: bool = True
) -> str:
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
    # use_semantic=False（源端）时语义类型一律不参与：驱动只认物理类型。
    st = (col.semantic_type or "").lower().strip() if use_semantic else ""
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


def _cdc_source_props(
    endpoint: FlinkEndpoint, physical: str
) -> dict[str, str]:
    """CDC 源表的 WITH (...)。用 mysql-cdc / postgres-cdc 连接器：hostname/port 拆开
    （不吃 JDBC url），凭据走 host/port/database 占位符（见 base.py 新增的键）。

    ``physical`` 是源库真实 ``库.表``。mysql-cdc 要 database-name + table-name（裸表名）；
    postgres-cdc 额外要 schema-name（postgres 的库内 schema），故 ``库.schema.表`` 与
    ``库.表`` 两种形状都要认。库名不从 physical 取而用 Connection 的 schema 占位符——
    连的是哪个库属部署事实（与 JDBC 系一致）。
    """
    platform = endpoint.platform.lower()
    connector = _CDC_CONNECTORS[platform]
    alias = endpoint.alias
    table_name = physical.rsplit(".", 1)[-1]  # 裸表名
    props = {
        "connector": connector,
        "hostname": _placeholder(alias, "HOSTNAME"),
        "port": _placeholder(alias, "PORT"),
        "username": _placeholder(alias, "USER"),
        "password": _placeholder(alias, "PASSWORD"),
        "database-name": _placeholder(alias, "DATABASE"),
        "table-name": table_name,
    }
    if connector == "postgres-cdc":
        # postgres 的库内 schema：physical 形如 库.schema.表 时取中段，否则默认 public。
        segs = physical.split(".")
        schema = segs[-2] if len(segs) >= 2 else "public"
        props["schema-name"] = schema
        # postgres-cdc 需要一个逻辑复制槽名；按表稳定生成，避免多作业抢同一个槽。
        props["slot.name"] = "ontometa_" + re.sub(r"[^a-z0-9_]", "_", table_name.lower())
    return props


def _connector_props(
    table: LogicalTable,
    endpoint: FlinkEndpoint,
    physical_name: str | None = None,
    *,
    cdc: bool = False,
    delete_policy: str = "ignore",
) -> dict[str, str]:
    """按平台产 WITH (...) 的连接属性。凭据一律占位符。

    ``physical_name`` 是数仓里的真实库表名（``库.表``），用作 JDBC ``table-name`` /
    Doris ``table.identifier``。与 Flink 逻辑表名（``table.name``，CREATE TABLE 用）分开：
    transform 里源实体与目标表常同名，Flink 逻辑名靠前缀区开，而物理名各指各的库。
    不给则回退 ``table.qualified_name``。

    ``cdc=True`` 时源表用 CDC 连接器（mysql-cdc/postgres-cdc）——仅对 CDC/增量作业的**源端**。
    """
    platform = endpoint.platform.lower()
    alias = endpoint.alias
    physical = physical_name or table.qualified_name

    if cdc:
        if platform not in _CDC_CONNECTORS:
            raise ValueError(
                f"平台 {platform!r} 无 Flink CDC 连接器（支持："
                f"{', '.join(sorted(_CDC_CONNECTORS))}）"
            )
        return _cdc_source_props(endpoint, physical)

    connector = _CONNECTORS.get(platform, "jdbc")
    if connector == "doris":
        props = {
            "connector": "doris",
            "fenodes": _placeholder(alias, "FENODES"),
            "table.identifier": physical,
            "username": _placeholder(alias, "USER"),
            "password": _placeholder(alias, "PASSWORD"),
            # 每次运行一个 label 前缀。Doris 的 stream load 按 label 去重，连接器默认
            # 从表名派生前缀，于是**同一张表的第二次运行**必然
            # ``[LABEL_ALREADY_EXISTS] Label [...] has already been used``——第一次跑成功
            # 之后就再也搬不了第二次。值由 Airflow 运行期给（见 endpoint_credential_env），
            # 带 DagRun 时刻与重试次数，故重跑、重试都各是一个新 label。
            "sink.label-prefix": _placeholder(alias, "LOAD_LABEL"),
        }
        if endpoint.benodes:
            # 配了 BE 地址就直接用，不再问 FE：容器化 Doris 的 BE 在 FE 里登记的是
            # 127.0.0.1，集群外的 Flink 照着连只会连到自己（Connection refused，
            # 报在作业运行期）。没配则不发这个属性，维持连接器问 FE 的老路。
            props["benodes"] = _placeholder(alias, "BENODES")
        if delete_policy == "hard_delete":
            props["sink.enable-delete"] = "true"
        return props
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
    cdc: bool = False,
    delete_policy: str = "ignore",
) -> str:
    """渲染一张表的 Flink SQL ``CREATE TABLE ... WITH (...)``。

    Args:
        table: 逻辑表（列、注释）；``table.name`` 作 Flink 逻辑表名
        endpoint: 物理端点（别名 + 平台）
        physical_name: 数仓真实库表名（JDBC table-name），缺省用 ``table.qualified_name``
        watermark: streaming 源表的 ``(列名, 策略)``
        cdc: 源端用 CDC 连接器（mysql-cdc/postgres-cdc），仅 CDC/增量作业的源表
    """
    col_lines = [
        # 源表用 source_flink_type（物理类型，语义不参与）、目标表用 target_flink_type
        # （引擎 Adapter）。两边必须与 build_identity_select 判 CAST 时用的是同两个函数，
        # 否则会出现「投影里 CAST(x AS BOOLEAN)、源表却声明成 BOOLEAN」这种自相矛盾：
        # 提交期看不出来，运行期在 TaskManager 里 ClassCastException。
        f"  `{c.name}` "
        + (target_flink_type(c, endpoint.platform) if is_target else source_flink_type(c))
        + (f" COMMENT '{c.comment}'" if c.comment else "")
        for c in table.columns
    ]
    if watermark:
        col_lines.append(f"  WATERMARK FOR `{watermark[0]}` AS {watermark[1]}")
    cols = ",\n".join(col_lines)

    props = _connector_props(
        table, endpoint, physical_name, cdc=cdc, delete_policy=delete_policy
    )
    props_str = ",\n  ".join(f"'{k}' = '{v}'" for k, v in props.items())
    return f"CREATE TABLE `{table.name}` (\n{cols}\n) WITH (\n  {props_str}\n);"


def build_identity_select(
    source_table: LogicalTable,
    target_table: LogicalTable,
    target_engine: str,
    *,
    column_map: dict[str, str] | None = None,
) -> str:
    """搬运（无清洗/无聚合）的恒等 SELECT 体：源列 → 目标列，类型不同则 CAST。

    这是「搬运 = 恒等投影的 transform」在生成器层的落地：transform 会在这个 body 上再叠
    清洗规则，metric 叠聚合，而搬运（sync/materialize）就用它本身。

    **类型不同必须 CAST**：源列类型由源库物理类型决定，
    目标列类型由本体语义经 Adapter 决定，两者本就允许不同——不 CAST 时 Flink 会在提交期拒绝
    （Column types of query result and sink do not match）或运行期 ClassCastException。

    ``column_map``：目标列名 → 源物理列名。缺省或某列未列出时，按同名映射（源列名 = 目标列名）。
    FROM 引用 ``source_table.name``（Flink 逻辑裸表名），与 :func:`generate_flink_sql` 的约定一致。
    """
    column_map = column_map or {}
    lines = []
    for tgt_col in target_table.columns:
        src_name = column_map.get(tgt_col.name) or tgt_col.name
        # 源端按物理类型、目标端按引擎 Adapter，各算各的（见 source/target_flink_type 说明）。
        src_type = source_flink_type(source_table.column(src_name) or tgt_col)
        tgt_type = target_flink_type(tgt_col, target_engine)
        expr = quote_identifier(src_name)
        if src_type != tgt_type:
            expr = f"CAST({expr} AS {tgt_type})"
        lines.append(f"  {expr} AS {quote_identifier(tgt_col.name)}")
    return "SELECT\n" + ",\n".join(lines) + f"\nFROM {quote_identifier(source_table.name)}"


def generate_move_sql(
    *,
    source_table: LogicalTable,
    target_table: LogicalTable,
    source: FlinkEndpoint,
    target: FlinkEndpoint,
    target_engine: str,
    mode: str = "full",
    column_map: dict[str, str] | None = None,
    source_physical: str | None = None,
    target_physical: str | None = None,
    checkpoint_dir: str | None = None,
    incremental_column: str | None = None,
    watermark: str | None = None,
    delete_policy: str = "ignore",
) -> str:
    """搬运作业（sync/materialize）的完整 Flink SQL——不含清洗/聚合，只把一张表从源搬到目标。

    统一执行架构的落地点：sync/materialize 与 transform/metric 走同一个生成器，差别只在
    这里用**恒等** SELECT 体（:func:`build_identity_select`），transform/metric 用带清洗/聚合的体。

    Args:
        mode: ``full`` → batch INSERT（有界，跑完即退）；
            ``incremental`` → 带增量字段/持久化水位谓词的有界 JDBC batch；
            ``cdc`` → mysql-cdc/postgres-cdc 常驻流作业。
        checkpoint_dir: 仅 CDC 作业使用；读位点存这里，重启从最近 checkpoint 续。

    与 :func:`generate_flink_sql` 的关系：本函数负责「搬运语义」（恒等体 + 全量/CDC 选择），
    组装仍委托给 generate_flink_sql，不重复 CREATE TABLE / SET 模式那套。
    """
    if mode not in ("full", "incremental", "cdc"):
        raise ValueError(f"未知装载方式 {mode!r}，可选：full / incremental / cdc")

    select_body = build_identity_select(
        source_table, target_table, target_engine, column_map=column_map
    )

    if mode == "full":
        return generate_flink_sql(
            source_table=source_table,
            target_table=target_table,
            source=source,
            target=target,
            select_body=select_body,
            execution_mode="batch",
            source_physical=source_physical,
            target_physical=target_physical,
            target_delete_policy=delete_policy,
        )

    if mode == "incremental":
        if not incremental_column or watermark in (None, ""):
            raise ValueError(
                "incremental 搬运必须配置 incremental_column 与成功/初始 watermark"
            )
        escaped = str(watermark).replace("'", "''")
        select_body += (
            f"\nWHERE {quote_identifier(incremental_column)} >= '{escaped}'"
        )
        # Incremental is a bounded JDBC batch. It must finish so Airflow can
        # quality-check and persist the next successful watermark.
        return generate_flink_sql(
            source_table=source_table,
            target_table=target_table,
            source=source,
            target=target,
            select_body=select_body,
            execution_mode="batch",
            source_physical=source_physical,
            target_physical=target_physical,
            target_delete_policy=delete_policy,
        )

    # CDC only: streaming + CDC source connector; position is persisted by
    # checkpoint/savepoint and the Airflow task submits detached.
    if source.platform.lower() not in _CDC_CONNECTORS:
        raise ValueError(
            f"源平台 {source.platform!r} 无 Flink CDC 连接器，无法做 CDC 搬运"
            f"（支持：{', '.join(sorted(_CDC_CONNECTORS))}）"
        )
    if not checkpoint_dir:
        # 不给 checkpoint 就没有读位点续存，重启即从头快照——违背增量语义，直接拦下。
        raise ValueError(
            "CDC 搬运是 streaming 作业，必须给 checkpoint_dir（file://…）"
            "以持久化读位点；否则重启会重搬。"
        )
    return generate_flink_sql(
        source_table=source_table,
        target_table=target_table,
        source=source,
        target=target,
        select_body=select_body,
        execution_mode="streaming",
        source_physical=source_physical,
        target_physical=target_physical,
        source_cdc=True,
        checkpoint_dir=checkpoint_dir,
        target_delete_policy=delete_policy,
    )


#: streaming 作业的 checkpoint 配置。SqlRunner 把非 CREATE/INSERT 的 SET 全塞进
#: ``tableEnv.getConfig().getConfiguration()``（见 SqlRunner.java），故这些 key 由它落地，
#: **Java 侧零改动**。1.13 的 key 名：state.backend=filesystem、state.checkpoints.dir 收 file://。
_CHECKPOINT_INTERVAL_MS = 10000  # 10s；CDC 作业读位点靠 checkpoint 续，间隔即最大重放窗口。


def _checkpoint_sets(checkpoint_dir: str) -> list[str]:
    """streaming/CDC 作业的 checkpoint SET 语句。``checkpoint_dir`` 形如 ``file:///var/…``
    （本地文件存储，standalone 与 YARN 通用；YARN 只改提交目标，不影响这些运行期配置）。"""
    return [
        f"SET 'execution.checkpointing.interval' = '{_CHECKPOINT_INTERVAL_MS}';",
        "SET 'state.backend' = 'filesystem';",
        f"SET 'state.checkpoints.dir' = '{checkpoint_dir}';",
    ]


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
    source_cdc: bool = False,
    checkpoint_dir: str | None = None,
    target_delete_policy: str = "ignore",
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
        source_cdc: 源表用 CDC 连接器（mysql-cdc/postgres-cdc）；仅 CDC/增量搬运
        checkpoint_dir: streaming 作业的 checkpoint 目录（``file://…``）；给了就注入 checkpoint SET

    Returns:
        完整 Flink SQL 脚本（幂等）
    """
    mode = "streaming" if execution_mode == "streaming" else "batch"
    watermark = source_watermark if mode == "streaming" else None

    parts = [f"SET 'execution.runtime-mode' = '{mode}';"]
    # checkpoint 只对 streaming 有意义（批作业跑完即退，无状态可存）。
    if mode == "streaming" and checkpoint_dir:
        parts += _checkpoint_sets(checkpoint_dir)
    parts += [
        "",
        "-- 源表",
        render_create_table(
            source_table, source, physical_name=source_physical,
            watermark=watermark, cdc=source_cdc,
        ),
        "",
        "-- 目标表",
        render_create_table(
            target_table,
            target,
            physical_name=target_physical,
            is_target=True,
            delete_policy=target_delete_policy,
        ),
        "",
        "-- 计算并写入",
        f"INSERT INTO `{target_table.name}`",
        select_body.rstrip().rstrip(";") + ";",
    ]
    return "\n".join(parts)
