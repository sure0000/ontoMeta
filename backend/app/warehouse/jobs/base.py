"""Flink 搬运作业的数据模型。

与 ``LogicalSchema`` 的分工：``LogicalTable`` 描述**目标表长什么样**（交给 Dialect Adapter
渲染 DDL），``JobSpec`` 描述**数据怎么从源搬到目标**（交给 Flink 渲染作业配置）。
二者都由「本体 + 物化契约」编译而来，共用同一份列映射事实源，不允许各算各的。

**约束**（与 ``adapters/`` 同构，违反即失去可移植性）：

- 搬运逻辑集中在 Flink 适配器与 SQL 编译器；规划器只处理平台能力和数据契约。
- **凭据绝不进 JobSpec**：只存数据源别名，连接串由执行侧按别名解析。
  比照 ``DataSource.dsn_secret_ref``「仅存引用」与 ``agents/executors/sync.py`` 的既有做法。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.warehouse.logical_schema import LogicalTable

# 装载方式，取值与 ``MaterializationContract.load_strategy`` 一致。
LOAD_MODES = ("full", "incremental", "cdc")

# 平台 → JDBC url 的 scheme。只覆盖 JDBC 系；不在表里的（hive 等）不产 URL。
_JDBC_URL_SCHEMES: dict[str, str] = {
    "mysql": "mysql",
    "mariadb": "mariadb",
    "postgres": "postgresql",
    "postgresql": "postgresql",
    "mssql": "sqlserver",
}


def _alias_token(alias: str) -> str:
    """别名 → 占位符/环境变量里的大写 token。``erp_readonly`` → ``ERP_READONLY``。"""
    return "".join(c if c.isalnum() else "_" for c in alias).strip("_").upper()


def _conn_attr(conn: str, attr: str) -> str:
    """Connection 某个字段的 Jinja 表达式，**None 渲染成空串而不是 "None"**。

    Airflow 的 Jinja 不设 finalize：``{{ conn.x.password }}`` 在密码为 NULL 时渲染出
    字面量 ``None``，于是环境变量真的带着四个字母交给连接器。Doris 默认的 root 就是
    空密码，报出来的是 ``Access denied for user 'root@…' (using password: YES)``——
    看着像密码错了，其实是我们发了一个叫 "None" 的密码。host/schema 同理：库名 None
    会拼进 JDBC url 变成 ``/None``。
    """
    expr = f"{conn}.{attr}"
    return f"{{{{ {expr} if {expr} is not none else '' }}}}"


def endpoint_credential_env(alias: str, platform: str) -> dict[str, str]:
    """一端的凭据环境变量 → Airflow 运行期 Jinja 表达式。

    **对外公开**：Flink 计算任务（transform/metric）与搬运作业用的是同一套占位符约定
    （``${别名_URL}`` / ``_USER`` / ``_PASSWORD``），两处各写一份迟早对不上——Flink 侧
    只要占位符名字差一个字，运行期就是「缺少凭据环境变量」。
    """
    return _endpoint_env(_EnvEndpoint(alias=alias, platform=platform))


@dataclass(frozen=True)
class _EnvEndpoint:
    """只为复用 _endpoint_env 的最小端点（Flink 侧没有表名概念）。"""

    alias: str
    platform: str


def _endpoint_env(endpoint) -> dict[str, str]:
    """一端的凭据环境变量 → Airflow 运行期 Jinja 表达式。"""
    token = _alias_token(endpoint.alias)
    conn = f"conn.{endpoint.alias}"
    env = {
        f"{token}_USER": _conn_attr(conn, "login"),
        f"{token}_PASSWORD": _conn_attr(conn, "password"),
        # host/port 拆开的占位符：Flink CDC 源连接器（mysql-cdc/postgres-cdc）不吃 JDBC url，
        # 要 hostname/port 分开的字段。**只新增、不改上面的 _URL**——JDBC 系（transform/metric
        # 的 sink、全量搬运）仍用 _URL，两套并存，互不影响。值同样是运行期 Jinja 表达式。
        f"{token}_HOSTNAME": _conn_attr(conn, "host"),
        f"{token}_PORT": _conn_attr(conn, "port"),
        # CDC 源要单独的库名（database-name）与库内 schema（postgres 的 schema-name），
        # 从 Connection 的 schema 段取。mysql-cdc 只用 database-name，postgres-cdc 两者都用。
        f"{token}_DATABASE": _conn_attr(conn, "schema"),
    }
    scheme = _JDBC_URL_SCHEMES.get((endpoint.platform or "").lower())
    if scheme:
        # 库名取 Connection 的 schema，而不是 JobSpec 里的目标库——连的是哪个库属于
        # 部署事实，由建 Connection 的人说了算。
        env[f"{token}_URL"] = (
            f"jdbc:{scheme}://{_conn_attr(conn, 'host')}:{_conn_attr(conn, 'port')}"
            f"/{_conn_attr(conn, 'schema')}"
        )
    if (endpoint.platform or "").lower() == "doris":
        # Doris connector writes through FE HTTP (8030), not the SQL port.
        # Keep it in Airflow Connection.extra; the generated SQL only contains
        # ${ALIAS_FENODES}, never the endpoint or credentials.
        env[f"{token}_FENODES"] = (
            f"{{{{ {conn}.extra_dejson.get('fenodes', '') }}}}"
        )
        # BE 的 HTTP 地址（可选）。只有设置页配了 benodes 时生成的 SQL 才会引用
        # ${ALIAS_BENODES}；这里始终给出表达式，没配就是空串，不引用也无害。
        env[f"{token}_BENODES"] = (
            f"{{{{ {conn}.extra_dejson.get('benodes', '') }}}}"
        )
        # stream load 的 label 前缀：**每次运行必须不同**。Doris 按 label 去重事务，
        # 连接器默认的前缀由表名派生，于是同一张表第二次搬运就是
        # ``LABEL_ALREADY_EXISTS``——首次成功之后再也搬不动。用 DagRun 的时刻 + 本次
        # 重试次数：重跑是新 DagRun（新时刻），DAG 内重试是新 try_number，都不会重名。
        env[f"{token}_LOAD_LABEL"] = "ontometa_{{ ts_nodash }}_{{ ti.try_number }}"
        env[f"{token}_JDBC_URL"] = (
            f"{{{{ {conn}.extra_dejson.get('jdbc_url', '') }}}}"
        )
    if (endpoint.platform or "").lower() == "hive":
        env[f"{token}_METASTORE_URI"] = (
            f"{{{{ {conn}.extra_dejson.get('metastore_uri', '') }}}}"
        )
        # 非 Kerberos 的 HDFS 就按这个环境变量认人。搬运容器里的用户不是建仓目录的
        # 那个（默认 hadoop:supergroup drwxr-xr-x），不传就是写 staging 目录时
        # ``AccessControlException: Permission denied: user=…, access=WRITE``
        # ——报错常出现在目标端提交阶段，表面像执行器故障，其实是身份配置问题。
        # **变量名必须原样是 HADOOP_USER_NAME**（Hadoop 客户端只认这个），故不加别名前缀；
        # 一个作业只有一端是 hive，不会互相覆盖。具体用户名属部署事实，从 Connection 的
        # extra 取（``{"hadoop_user": "hadoop"}``），没配则用 Hadoop 镜像的惯例用户。
        env["HADOOP_USER_NAME"] = (
            f"{{{{ {conn}.extra_dejson.get('hadoop_user', 'hadoop') }}}}"
        )
    return env


@dataclass(frozen=True)
class JobEndpoint:
    """作业的一端（源或目标）。

    ``alias`` 是**唯一**的凭据线索：执行侧按别名解析出连接串，本对象里不含任何
    主机、账号、密码。``platform`` 决定连接器插件（mysql/mariadb/hive/doris…），
    取自 DataHub URN 的 dataPlatform 段或目标引擎名。
    """

    alias: str
    platform: str
    table: str
    database: str | None = None

    @property
    def qualified(self) -> str:
        return f"{self.database}.{self.table}" if self.database else self.table


@dataclass(frozen=True)
class ColumnMapping:
    """一列的搬运映射：源列 → 目标列（目标列名 = 本体属性名）。"""

    source: str
    target: str


@dataclass(frozen=True)
class JobSpec:
    """一张表的搬运作业声明。

    ``source_urn`` 原样保留本体的 ``ObjectType.source_ref``（本就是 DataHub URN），
    供 M11 上报血缘时直接用作上游标识。**目标侧 URN 不在此构造**——它需要部署环境
    （PROD/DEV 等 fabric）才能确定，M9 无从得知，硬编一个 PROD 只会埋错。
    """

    name: str  # 作业名，稳定且可直接用作 Airflow task_id
    source: JobEndpoint
    target: JobEndpoint
    columns: tuple[ColumnMapping, ...]
    mode: str = "full"  # LOAD_MODES 之一
    # 分区键；增量装载时同时作为水位列（与 M3 生成 ETL 的口径一致）。
    partition_key: str | None = None
    incremental_column: str | None = None
    initial_watermark: str | None = None
    delete_policy: str = "ignore"
    # 数仓分层，仅用于分组与并发闸门，不表达数据依赖（见下方 note）。
    layer: str = "dim"
    source_urn: str | None = None
    # 承载该表的本体实体技术名（= LogicalTable.source_name）。物理表名可被弹窗改写，
    # 而按 refresh_cron 分组要回查契约，须用实体名而非物理表名。空 = 与源表名同。
    entity_name: str | None = None
    # 目标表的**带类型**逻辑表（列的 data_type/semantic_type）。统一执行架构把搬运编译成
    # Flink SQL 需要列类型（源端物理类型、目标端引擎类型都从这里算，见 flink_sql_generator），
    # 而 ``columns`` 只有名字映射。planner 建计划时本就持有它（build_logical_schema 的产物），
    # 顺手带上，避免下游再全量重建一次 schema。纯手工构造 JobSpec 时可省略。
    target_table: LogicalTable | None = None

    def __post_init__(self) -> None:
        if self.mode not in LOAD_MODES:
            raise ValueError(f"未知装载方式 {self.mode!r}，可选：{', '.join(LOAD_MODES)}")


@dataclass
class JobPlan:
    """一次物化的全部搬运作业 + 不可搬运项。

    **搬运作业之间没有数据依赖**：每个作业各自从源系统读、写各自的目标表，彼此独立。
    ``layer`` 只用于分组（层间串行是为并发闸门与失败早停，不是依赖）。这一点与
    ``generate_dag()`` 表达的**表间**依赖是两回事——那张图描述的是派生关系
    （ADS 由 DWD 算出），而派生不走搬运作业，由 MetricSpec/Transform 负责。
    刻意不在此复制那张稠密图：真实 ERP 本体 734 个对象，按层两两连边会产生 O(n²) 条边。
    """

    jobs: tuple[JobSpec, ...] = ()
    # 产不出搬运作业的表及原因（缺 source_ref、CDC 无连接器…）。
    unsupported: list[dict] = field(default_factory=list)
    # 逻辑计划（M3）在编译目标表时留下的提示，原样带出不吞。
    # **与 unsupported 分开**：那些说的是「表结构上有什么问题」（如缺主键），
    # 多数并不妨碍搬运——混在一起会让人误以为几百张表搬不了。
    schema_notes: list[dict] = field(default_factory=list)

    def note(self, target: str, reason: str) -> None:
        self.unsupported.append({"target": target, "reason": reason})
