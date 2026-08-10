"""搬运作业的工具无关表示（M9）。

与 ``LogicalSchema`` 的分工：``LogicalTable`` 描述**目标表长什么样**（交给 Dialect Adapter
渲染 DDL），``JobSpec`` 描述**数据怎么从源搬到目标**（交给 SyncTool Adapter 渲染作业配置）。
二者都由「本体 + 物化契约」编译而来，共用同一份列映射事实源，不允许各算各的。

**约束**（与 ``adapters/`` 同构，违反即失去可移植性）：

- 本模块与各 SyncTool Adapter 之外的代码**不得包含任何搬运工具特定逻辑**——
  一旦 ``if tool == "seatunnel"`` 出现在 planner 里，工具可插拔就名存实亡了。
- **凭据绝不进 JobSpec**：只存数据源别名，连接串由执行侧按别名解析。
  比照 ``DataSource.dsn_secret_ref``「仅存引用」与 ``agents/executors/sync.py`` 的既有做法。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
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
        f"{token}_USER": f"{{{{ {conn}.login }}}}",
        f"{token}_PASSWORD": f"{{{{ {conn}.password }}}}",
        # host/port 拆开的占位符：Flink CDC 源连接器（mysql-cdc/postgres-cdc）不吃 JDBC url，
        # 要 hostname/port 分开的字段。**只新增、不改上面的 _URL**——JDBC 系（transform/metric
        # 的 sink、全量搬运）仍用 _URL，两套并存，互不影响。值同样是运行期 Jinja 表达式。
        f"{token}_HOSTNAME": f"{{{{ {conn}.host }}}}",
        f"{token}_PORT": f"{{{{ {conn}.port }}}}",
        # CDC 源要单独的库名（database-name）与库内 schema（postgres 的 schema-name），
        # 从 Connection 的 schema 段取。mysql-cdc 只用 database-name，postgres-cdc 两者都用。
        f"{token}_DATABASE": f"{{{{ {conn}.schema }}}}",
    }
    scheme = _JDBC_URL_SCHEMES.get((endpoint.platform or "").lower())
    if scheme:
        # 库名取 Connection 的 schema，而不是 JobSpec 里的目标库——连的是哪个库属于
        # 部署事实，由建 Connection 的人说了算。
        env[f"{token}_URL"] = (
            f"jdbc:{scheme}://{{{{ {conn}.host }}}}:{{{{ {conn}.port }}}}"
            f"/{{{{ {conn}.schema }}}}"
        )
    if (endpoint.platform or "").lower() == "hive":
        env[f"{token}_METASTORE_URI"] = (
            f"{{{{ {conn}.extra_dejson.get('metastore_uri', '') }}}}"
        )
        # 非 Kerberos 的 HDFS 就按这个环境变量认人。搬运容器里的用户不是建仓目录的
        # 那个（默认 hadoop:supergroup drwxr-xr-x），不传就是写 staging 目录时
        # ``AccessControlException: Permission denied: user=…, access=WRITE``
        # ——报错在 SeaTunnel 的 commit 阶段，读起来像 SeaTunnel 的问题，其实是身份问题。
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
    # 数仓分层，仅用于分组与并发闸门，不表达数据依赖（见下方 note）。
    layer: str = "dim"
    source_urn: str | None = None
    # 承载该表的本体实体技术名（= LogicalTable.source_name）。物理表名可被弹窗改写，
    # 而按 refresh_cron 分组要回查契约，须用实体名而非物理表名。空 = 与源表名同。
    entity_name: str | None = None
    # 目标表的**带类型**逻辑表（列的 data_type/semantic_type）。统一执行架构把搬运编译成
    # Flink SQL 需要列类型（源端物理类型、目标端引擎类型都从这里算，见 flink_sql_generator），
    # 而 ``columns`` 只有名字映射。planner 建计划时本就持有它（build_logical_schema 的产物），
    # 顺手带上，避免下游再全量重建一次 schema。旧的 docker/runner 渲染不读它，故可选、默认 None。
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


class SyncToolAdapter(ABC):
    """搬运工具适配器。工具特定逻辑**只允许**存在于其实现中。

    除了把 JobSpec 渲染成作业配置（``render``），适配器还声明**怎么让 Airflow 跑它**：
    ``docker_image`` 是执行镜像，``airflow_command`` 是 DockerOperator 的命令。镜像/命令
    属于工具本身（不同工具不同），故收拢到适配器而非设置页——设置页只管 Airflow 怎么连。
    """

    name: str
    # 该工具的默认执行镜像（DockerOperator 用）。与 docker/orchestration 的镜像保持一致。
    docker_image: str
    # 该镜像是否**公开可拉取**。False 表示 ``docker_image`` 只是个占位名字，
    # registry 上并不存在（DataX 就没有官方镜像），部署方必须自建并用
    # ``SYNC_TOOL_IMAGES`` 指过来。区分这一位是为了**在提交前拦下**：
    # 否则 DAG 照样生成、照样触发，三分钟后才在 Airflow 里炸一个
    # ``pull access denied for …, repository does not exist``（真实实例上踩过），
    # 而那条报错读起来像环境故障，不像「这个工具在本部署里根本没配」。
    has_official_image: bool = True
    # 作业配置挂进容器的目录。各工具的约定目录不同，故随工具走——
    # 把 SeaTunnel 的 /opt/seatunnel/jobs 挂进 DataX 容器不会报错，但产物里
    # 出现另一个工具的路径，是后来人读 spec 时的纯噪音。
    jobs_mount_dir: str = ""
    # 该工具在容器里加载第三方 jar 的目录。JDBC 驱动因授权原因不随镜像分发
    # （SeaTunnel/Flink 官方镜像都不带），需由部署方提供并挂进这里，否则
    # ``ClassNotFoundException: …jdbc.Driver``。放适配器是因为各工具目录不同。
    driver_lib_dir: str = ""

    @abstractmethod
    def render(self, job: JobSpec) -> dict:
        """渲染成该工具的作业配置。

        实现须保证：**幂等**（同一 JobSpec 重复渲染逐字节一致）、**无凭据**
        （只出现别名派生的占位符）。
        """

    @abstractmethod
    def supports(self, mode: str) -> bool:
        """该工具是否支持此装载方式。不支持的必须显式返回 False，不静默降级。"""

    @abstractmethod
    def airflow_command(
        self, config_path: str, variables: dict[str, str] | None = None
    ) -> list[str]:
        """DockerOperator 跑该工具的命令。``config_path`` 是容器内的作业配置路径。

        水位用 Airflow 模板 ``{{ data_interval_start }}`` 注入（DockerOperator 的
        ``command`` 是模板字段，运行时会被渲染），各工具自定义怎么传。

        ``variables`` 是作业配置里 ``${…}`` 占位符的取值（见 :meth:`credential_env`，
        值同样是运行期才渲染的 Jinja 表达式）。**怎么把它交给工具是工具自己的事**：
        有的读环境变量，有的只认命令行参数，故在这里由各适配器决定。
        """

    def supports_cdc_from(self, platform: str) -> bool:
        """该工具能否对此源平台做 CDC。默认否——CDC 连接器是逐平台实现的，
        没实现就是没有，不能靠「工具号称支持 CDC」推断。"""
        return False

    def placeholder(self, alias: str, field_name: str) -> str:
        """别名 + 字段 → 执行期占位符，如 ``erp_readonly`` + ``URL`` → ``${ERP_READONLY_URL}``。

        这是「凭据不进产物」的落地形式：产物里只有占位符，实际值由执行侧注入
        （注入端见 :meth:`credential_env`）。
        """
        token = _alias_token(alias)
        return f"${{{token}_{field_name.upper()}}}"

    def credential_env(self, job: JobSpec) -> dict[str, str]:
        """占位符 → **运行期**取值表达式，交给 Airflow 注入进搬运容器。

        ``alias`` 按约定即 Airflow Connection 的 ``conn_id``（见 :class:`JobEndpoint`
        「执行侧按别名解析出连接串」）。这里产出的是 Jinja 表达式而非真实值：

        - 表达式在**任务运行时**才渲染，产物（DAG/作业配置/spec）里始终只有
          ``{{ conn.… }}``，凭据不落盘、不进 DAG 序列化；
        - 解析期不碰 Connection，避免每次 DAG 解析都打一次元数据库。

        ``METASTORE_URI`` 这类没法从 host/port 可靠推出来的字段，从 Connection 的
        ``extra`` 里按同名小写键取（如 ``{"metastore_uri": "thrift://host:9083"}``）。
        """
        env: dict[str, str] = {}
        for endpoint in (job.source, job.target):
            if not endpoint.alias:
                continue
            env.update(_endpoint_env(endpoint))
        return env
