"""搬运作业 → Flink SQL 任务的编译器（统一执行架构 B）。

把一个 :class:`JobSpec`（sync/materialize 的一张表搬运声明）编译成一个
:class:`FlinkSqlTask`（Airflow BashOperator 跑的 ``flink run`` 单元）。这是「搬运走
Flink、与 transform/metric 同一条执行路径」的落地接缝：

- SQL 体由 :func:`flink_sql_generator.generate_move_sql` 产（恒等投影，全量 batch /
  增量·cdc streaming）；
- 凭据走 :func:`endpoint_credential_env` 的占位符（两端都给，跨库作业缺一端即运行期
  「缺少凭据环境变量」），产物里只有 ``${别名_*}``。

**刻意与 DB 解耦**：入参是已经建好的 JobSpec（planner 的产物，带类型目标表），本模块不碰
Session、不重建 schema，故可脱离 DB 单元测试。
"""

from __future__ import annotations

from app.services.flink_sql_generator import FlinkEndpoint, generate_move_sql
from app.services.flink_sql_lineage import task_lineage_urns
from app.warehouse.jobs.base import JobSpec, endpoint_credential_env
from app.warehouse.logical_schema import LogicalColumn, LogicalTable

# Airflow DAG 构建层的任务载体。放在函数级 import 会更干净，但这里模块级也无环
# （airflow_dag_builder 不反过来 import 本模块）。
from app.services.airflow_dag_builder import FlinkSqlTask


def _source_logical_table(job: JobSpec) -> LogicalTable:
    """从带类型的目标表 + 列名映射，重建**源端**逻辑表。

    源列名 = 源物理列名（JDBC/CDC 从物理表读，列名须匹配），类型沿用目标列的
    ``data_type``/``semantic_type``——这与 ``build_flink_etl_input`` 同口径：本体记录的
    列类型即源库物理类型，源端按物理类型声明、目标端再经引擎 Adapter 映射，
    :func:`build_identity_select` 两边算出的类型不同就 CAST。

    Flink 逻辑表名加 ``src_`` 前缀，避免与目标表（同名实体常见）在同一脚本里撞名。
    """
    if job.target_table is None:
        raise ValueError(
            f"作业 {job.name} 缺 target_table（带类型目标表）；planner 应已填充，"
            "无类型无法编译 Flink SQL 搬运。"
        )
    # 目标列名 → 源物理列名。ColumnMapping 是 (source=物理, target=本体名)，这里要
    # 「本体名 → 物理」，即按 target 取 source。
    col_map = {c.target: c.source for c in job.columns}
    source_columns = tuple(
        LogicalColumn(
            name=col_map.get(c.name) or c.name,
            data_type=c.data_type,
            semantic_type=c.semantic_type,
            comment=c.comment,
            nullable=c.nullable,
        )
        for c in job.target_table.columns
    )
    return LogicalTable(
        name=f"src_{job.entity_name or job.target_table.name}",
        database=None,  # Flink 表无 database 概念，物理名单独经 source_physical 指定
        layer="ods",
        columns=source_columns,
    )


def compile_move_task(
    job: JobSpec,
    *,
    engine: str,
    checkpoint_dir: str | None = None,
    target_benodes: bool = False,
) -> FlinkSqlTask:
    """一个搬运 JobSpec → 一个 FlinkSqlTask。

    Args:
        job: 搬运声明（planner 产物，须带 ``target_table``）。
        engine: 目标引擎（hive/doris/postgres…），决定目标列类型与 sink 连接器。
        checkpoint_dir: CDC 作业的 checkpoint 目录（``file://…``）；full/incremental 忽略。
            新 incremental 契约使用持久化水位的有界 batch；历史无水位契约保留 CDC 兼容重放。
        target_benodes: 目标 Doris 是否已在设置页配了 BE 地址。配了才在 sink 上发
            ``benodes``（部署事实由调用方查，planner/编译器不查库）。

    Returns:
        FlinkSqlTask：task_id = 作业名；sql = 完整 Flink SQL；env = 两端凭据占位符表达式；
        detached = 是否流式（增量/cdc streaming 提交即 detach，全量 batch 阻塞到完成）。
    """
    source_table = _source_logical_table(job)
    target_table = job.target_table

    column_map = {c.target: c.source for c in job.columns}
    # Historical incremental JobSpecs predate IngestionContract and carry no
    # persisted watermark. Keep their former CDC-streaming behavior for replay;
    # every new Doris ODS contract supplies incremental_column+initial_watermark
    # and therefore takes the bounded batch branch.
    effective_mode = (
        "cdc"
        if job.mode == "incremental"
        and not job.incremental_column
        and job.initial_watermark in (None, "")
        else job.mode
    )
    sql = generate_move_sql(
        source_table=source_table,
        target_table=target_table,
        source=FlinkEndpoint(job.source.alias, job.source.platform),
        target=FlinkEndpoint(job.target.alias, job.target.platform, benodes=target_benodes),
        target_engine=engine,
        mode=effective_mode,
        column_map=column_map,
        source_physical=job.source.qualified,
        target_physical=job.target.qualified,
        checkpoint_dir=checkpoint_dir,
        incremental_column=(
            column_map.get(job.incremental_column or job.partition_key or "")
            or job.incremental_column
            or job.partition_key
        ),
        watermark=job.initial_watermark,
        delete_policy=job.delete_policy,
    )
    # 跨库作业两端各是一个 Airflow Connection，两端凭据都要给（少一端即运行期缺变量）。
    env = {
        **endpoint_credential_env(job.source.alias, job.source.platform),
        **endpoint_credential_env(job.target.alias, job.target.platform),
    }
    # L1 血缘：**优先用 job.source_urn**（本体的 ObjectType.source_ref，本就是 DataHub
    # URN，M11 血缘上报也用它）——比从物理名构造更准（物理名可能被弹窗改写）。
    # 没有 source_urn 时退回从物理名构造。目标侧物理名是确定的（job.target.qualified），
    # 直接转 URN；fabric 是部署事实，此处用默认 PROD（调用方如知 fabric 可覆盖）。
    if job.source_urn:
        source_urns = (job.source_urn,)
    else:
        source_urns, _ = task_lineage_urns(
            source_tables=[job.source.qualified],
            target_table=None,
            source_platform=job.source.platform or "hive",
            target_platform=engine,
        )
    _, target_urn = task_lineage_urns(
        source_tables=[],
        target_table=job.target.qualified,
        source_platform=job.source.platform or "hive",
        target_platform=engine,
    )
    # 全量是有界批作业（跑完即退，SqlRunner await 到结束）；增量/cdc 是常驻流式，
    # 提交即 detach（-d），否则 Airflow 任务会永远阻塞在一个不会结束的流作业上。
    detached = effective_mode == "cdc"
    return FlinkSqlTask(
        task_id=job.name,
        sql=sql,
        env=env,
        detached=detached,
        source_urns=source_urns,
        target_urn=target_urn,
    )
