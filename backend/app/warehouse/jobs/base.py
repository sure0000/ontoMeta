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

# 装载方式，取值与 ``MaterializationContract.load_strategy`` 一致。
LOAD_MODES = ("full", "incremental", "cdc")


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
    def airflow_command(self, config_path: str) -> list[str]:
        """DockerOperator 跑该工具的命令。``config_path`` 是容器内的作业配置路径。

        水位用 Airflow 模板 ``{{ data_interval_start }}`` 注入（DockerOperator 的
        ``command`` 是模板字段，运行时会被渲染），各工具自定义怎么传。
        """

    def supports_cdc_from(self, platform: str) -> bool:
        """该工具能否对此源平台做 CDC。默认否——CDC 连接器是逐平台实现的，
        没实现就是没有，不能靠「工具号称支持 CDC」推断。"""
        return False

    def placeholder(self, alias: str, field_name: str) -> str:
        """别名 + 字段 → 执行期占位符，如 ``erp_readonly`` + ``URL`` → ``${ERP_READONLY_URL}``。

        这是「凭据不进产物」的落地形式：产物里只有占位符，实际值由执行侧注入。
        """
        token = "".join(c if c.isalnum() else "_" for c in alias).strip("_").upper()
        return f"${{{token}_{field_name.upper()}}}"
