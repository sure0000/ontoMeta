"""runner 的版本化线格式（wire contract）。

ontoMeta 与 runner 是两个独立发版的进程，靠这份 schema 通信。**版本化**：runner 的
``GET /capabilities`` 返回 ``contract_version``，ontoMeta 只接受自己认识的版本、不匹配即
拒绝提交（不是发过去再看会不会炸，见 §3.5）。改动线格式必须同时 bump ``CONTRACT_VERSION``。

这份 schema 是 backend ``app/warehouse/jobs/base.py`` 里 JobSpec 的**投影**——只保留搬运真正
需要的字段，去掉血缘/分层等编排侧概念。两边刻意不共享代码：共享会把后端的依赖拖进 runner 镜像，
也让「独立发版」名存实亡。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# 线格式版本。任何字段的增删改都要 bump，ontoMeta 侧据此拒绝不认识的 runner。
# 2：Capabilities 增 sink_modes（按「目标 × 装载方式」声明，见该字段说明）。
# 3：JobStatus 增 backend（这张表实际由哪一档搬的）。
CONTRACT_VERSION = "3"

# 作业状态机。queued→running→(success|failed)。
QUEUED = "queued"
RUNNING = "running"
SUCCESS = "success"
FAILED = "failed"
TERMINAL_STATES = frozenset({SUCCESS, FAILED})


class WireColumn(BaseModel):
    """一列的搬运映射：源列 → 目标列（目标列名 = 本体属性名）。"""

    source: str
    target: str


class WireEndpoint(BaseModel):
    """作业的一端。``alias`` 是唯一的凭据线索，runner 按它自解析连接串——本对象里
    不含任何主机/账号/密码。``platform`` 决定用哪种连接（mysql/mariadb/postgres/doris…）。"""

    alias: str
    platform: str
    table: str
    database: str | None = None

    @property
    def qualified(self) -> str:
        return f"{self.database}.{self.table}" if self.database else self.table


class WireJobSpec(BaseModel):
    """一张表的搬运声明。凭据不在此，只有别名。"""

    name: str
    source: WireEndpoint
    target: WireEndpoint
    columns: list[WireColumn] = Field(default_factory=list)
    mode: str = "full"  # full | incremental（cdc 不由 native 承载）
    partition_key: str | None = None


class JobSubmit(BaseModel):
    """``POST /jobs`` 请求体。

    ``idempotency_key`` = ``dag_run_id + task_id``：同一个键重复提交返回同一个 job，
    重跑不会搬第二遍（§3.3 幂等）。``watermark`` 是增量装载的起点（Airflow 的
    ``data_interval_start``）；M15 会改为 runner 回读目标表，M14 先信调用方给的值。
    """

    spec: WireJobSpec
    idempotency_key: str
    watermark: str | None = None
    batch_size: int = 1000


class JobStatus(BaseModel):
    """``GET /jobs/{id}`` 回执。行数与水位让「跑成功了但没数据」当场可见（§3.3）。"""

    job_id: str
    idempotency_key: str
    state: str
    # 这张表实际由哪一档搬的（native / seatunnel）。档位是 runner 逐表自选的
    # （``backends.pick_backend``），选择结果只进过日志——于是「为什么这张表搬得慢/
    # 没有水位」在 ontoMeta 侧无从对账。排队中为 None（还没挑）。
    backend: str | None = None
    target: str = ""
    rows_read: int = 0
    rows_written: int = 0
    watermark_before: str | None = None
    watermark_after: str | None = None
    error: str | None = None


class Capabilities(BaseModel):
    """``GET /capabilities``。planner 据此判每张表可不可搬，不可搬的列进 unsupported，
    不静默降级（§3.1）。"""

    contract_version: str = CONTRACT_VERSION
    backends: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    sinks: list[str] = Field(default_factory=list)
    modes: list[str] = Field(default_factory=list)
    drivers: list[str] = Field(default_factory=list)
    # 「目标 × 装载方式」的实际组合。``sinks`` 与 ``modes`` 是两个扁平集合，分别判会**放过
    # 不存在的组合**：seatunnel 档能写 hive 但只做全量、native 档能做增量但写不了 hive，
    # 两个集合合起来看，「hive + 增量」会通过门禁、跑到执行侧才失败。这张表按组合声明，
    # planner 有它就按它判（没有则退回扁平集合，兼容旧 runner）。
    sink_modes: dict[str, list[str]] = Field(default_factory=dict)


class ProbeRequest(BaseModel):
    """``POST /probe``：这个 alias 连不连得通、能不能读到指定表。preflight 用。"""

    alias: str
    platform: str | None = None
    table: str | None = None
    database: str | None = None


class ProbeResult(BaseModel):
    """``POST /probe`` 的结果。

    ``checkable=False`` 是**第三种结论**，既不是通也不是不通：这个别名不由 runner 直连，
    因而探不了。典型是 Hive 目标——数据由 Zeta 集群经 metastore/HDFS 写入，runner 手上
    只有 metastore 地址，没有可连的 JDBC 串。把它当「连不通」会给出一个错误的诊断，
    让人去修一个根本不存在的连接串。
    """

    alias: str
    reachable: bool
    checkable: bool = True
    can_read_table: bool | None = None
    detail: str = ""
