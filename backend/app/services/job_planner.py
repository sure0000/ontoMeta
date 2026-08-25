"""本体 + 物化契约 → 搬运作业计划（M9）。

**为什么需要它**：现有物化把装载写成 ``INSERT OVERWRITE TABLE dim.customer
SELECT ... FROM erp_ods.tab_customer``（``warehouse_generator.generate_etl_sql``），
并在**目标数仓**的连接上执行——这隐含假设源表在目标数仓里可见。真实拓扑是源库
（ERP 的 MySQL/MariaDB）与数仓分处两侧，除非配了外部 Catalog，这条 SQL 必然报表不存在。
跨库搬运本就不是一条 INSERT…SELECT 能干的事，故改由专业搬运工具执行，本模块负责
把「搬什么、从哪到哪、怎么搬」编译成工具无关的 ``JobSpec``。

**同源约束**：列映射、装载方式、分区键全部取自与 M3 相同的事实源
（``LogicalTable`` + ``warehouse_generator._field_refs``），不另算一套——否则
「生成的 DDL」与「搬运的数据」迟早对不上。

**建表不归这里**：目标表由 M3 的 DDL 建（本体反补的注释/分区/主键声明只在那条路径上）。
搬运工具的 auto-create schema 必须关掉，否则这些语义会被悄悄绕过。
"""

from __future__ import annotations

from dataclasses import replace

from sqlalchemy.orm import Session

from app.services.source_ref import (
    NO_PHYSICAL_SOURCE_NOTE,
    has_physical_source,
    source_platform_of,
)
from app.warehouse.jobs import (
    ColumnMapping,
    JobEndpoint,
    JobPlan,
    JobSpec,
    get_job_adapter,
)
from app.services.warehouse_generator import WarehouseGenerator
from app.warehouse.logical_schema import LogicalConstraint

# 源/目标连接别名的缺省值。**别名不是凭据**：执行侧按别名解析连接串，
# 沿用 ``agents/drafters/sync.py`` 里 ``source_ref_alias`` 的既有约定。
DEFAULT_SOURCE_ALIAS = "erp_readonly"
DEFAULT_TARGET_ALIAS = "warehouse_default"

_generator = WarehouseGenerator()


class JobPlanningError(ValueError):
    """搬运计划存在无法安全执行的作业冲突。"""


def _split_qualified(name: str) -> tuple[str | None, str]:
    """``_3214abce8e7be3d7.tabAddress`` → (库, 表)；无库名时库为 None。"""
    if "." in name:
        database, _, table = name.rpartition(".")
        return database or None, table
    return None, name


def _task_name(layer: str, table: str) -> str:
    """作业名。需能直接用作 Airflow task_id，故只留字母数字与下划线。"""
    raw = f"sync_{layer}_{table}"
    return "".join(c if c.isalnum() or c in "_-." else "_" for c in raw)


def _move_signature(job: JobSpec) -> tuple:
    """比较两个作业是否会执行同一次数据搬运。

    目标表的 comment / 外键属于建表元数据，不改变 Flink 搬运本身；列形状、读写端点、
    装载方式与水位策略则必须完全一致。这样对象表与以它为实现表的关系表在统一改写到
    同一 ODS 表后可以安全合并，而真正的同名异义冲突不会被静默吞掉。
    """
    target_columns = job.target_table.columns if job.target_table is not None else ()
    return (
        job.source,
        job.target,
        job.columns,
        job.mode,
        job.partition_key,
        job.incremental_column,
        job.initial_watermark,
        job.delete_policy,
        job.layer,
        job.source_urn,
        job.entity_name,
        target_columns,
    )


def _deduplicate_jobs(jobs: list[JobSpec]) -> tuple[JobSpec, ...]:
    """按最终 task id 与目标表去重，并在歧义写入前失败。

    Airflow 要求一个 DAG 内 task_id 唯一；数据侧也不允许两个不同作业并发写同一目标表。
    planner 是两条约束最后同时可见的边界，因此在这里收口，而不是等 DAG import 才暴露。
    """
    unique: list[JobSpec] = []
    by_name: dict[str, JobSpec] = {}
    by_target: dict[tuple[str, str, str], JobSpec] = {}

    for job in sorted(jobs, key=lambda item: (item.layer, item.name)):
        target_key = (job.target.alias, job.target.platform, job.target.qualified)
        collisions: list[JobSpec] = []
        for existing in (by_name.get(job.name), by_target.get(target_key)):
            if existing is not None and all(existing is not item for item in collisions):
                collisions.append(existing)

        if not collisions:
            unique.append(job)
            by_name[job.name] = job
            by_target[target_key] = job
            continue

        if len(collisions) == 1 and _move_signature(collisions[0]) == _move_signature(job):
            # 同一本体对象可能同时是对象表和关系实现表。ODS 同步把两者改写到相同的
            # 最终库表后，它们是同一次搬运，只保留稳定排序后的第一份。
            continue

        existing = collisions[0]
        raise JobPlanningError(
            "搬运作业冲突：Airflow task_id "
            f"{job.name!r} 或目标表 {job.target.qualified!r} 被多个不同作业占用"
            f"（{existing.source.qualified} -> {existing.target.qualified}；"
            f"{job.source.qualified} -> {job.target.qualified}）。"
            "请检查对象/关系物化契约与目标表覆盖配置。"
        )

    return tuple(unique)


def _runner_reject(
    capabilities: dict, mode: str, source_platform: str, target_engine: str
) -> str | None:
    """runner 通道的可搬性门禁：按 runner 如实声明的 capabilities 判，不静默降级。

    返回拒绝原因（进 unsupported）或 None（可搬）。替代 docker 通道那套「工具适配器
    硬编码平台表」——能不能搬由**执行侧实际装了什么**说了算（§3.1）。
    """
    modes = set(capabilities.get("modes") or [])
    sources = set(capabilities.get("sources") or [])
    sinks = set(capabilities.get("sinks") or [])
    sink_modes = capabilities.get("sink_modes") or {}
    engine = target_engine.lower()

    if source_platform.lower() not in sources:
        return f"runner 无 {source_platform} 源连接器（支持：{', '.join(sorted(sources)) or '无'}）"
    if engine not in sinks:
        return f"runner 无 {target_engine} 目标连接器（支持：{', '.join(sorted(sinks)) or '无'}）"

    if sink_modes:
        # 按**组合**判：扁平的 modes/sinks 分别判会放过实际不存在的组合——runner 可能
        # 一个档能写 hive 但只做全量、另一个档能做增量但写不了 hive，合看「hive + 增量」
        # 会通过门禁、跑到执行侧才失败（contract v2 起 runner 会声明 sink_modes）。
        allowed = set(sink_modes.get(engine) or [])
        if mode not in allowed:
            return (
                f"runner 写 {target_engine} 时不支持装载方式 {mode}"
                f"（该目标支持：{', '.join(sorted(allowed)) or '无'}）"
            )
        return None

    # 旧 runner（无 sink_modes）：只能退回扁平判断。
    if mode not in modes:
        return f"runner 不支持装载方式 {mode}（支持：{', '.join(sorted(modes)) or '无'}）"
    return None


class JobPlanner:
    def build(
        self,
        db: Session,
        ontology_id: str,
        *,
        engine: str,
        tool: str | None = None,
        source_alias: str = DEFAULT_SOURCE_ALIAS,
        target_alias: str = DEFAULT_TARGET_ALIAS,
        database_prefix: str | None = None,
        database_overrides: dict[str, str] | None = None,
        table_overrides: dict[str, str] | None = None,
        selected_targets: list[str] | None = None,
        runner_capabilities: dict | None = None,
        load_strategy: str | None = None,
        target_ods_database: str | None = None,
        target_ods_tables: dict[str, str] | None = None,
        target_primary_keys: dict[str, list[str]] | None = None,
        incremental_columns: dict[str, str] | None = None,
        initial_watermarks: dict[str, str] | None = None,
        source_physical_tables: dict[str, str] | None = None,
        source_platforms: dict[str, str] | None = None,
        source_mappings: dict[str, dict[str, str]] | None = None,
        delete_policies: dict[str, str] | None = None,
    ) -> JobPlan:
        """产出搬运作业计划。

        ``engine`` 为目标数仓引擎（决定 sink 连接器）；``database_overrides`` /
        ``table_overrides`` 与物化弹窗同义，保证作业写入的库表与 DDL 建的完全一致。
        ``selected_targets`` 按**本体实体名**裁剪（不是物理表名，故改过表名也不会误裁）。
        ``runner_capabilities`` 给了则走 runner 通道的可搬性门禁（按执行侧实际能力判），
        为 None 则沿用 docker 通道的工具适配器门禁。

        ``load_strategy``：**本次运行的全局装载方式覆盖**，缺省 None = 逐表按契约。
        给了它，物化 Spec 上那个 ``load_strategy`` 才真的作数——此前它传到 runner
        就没了，界面显示「全量覆盖」而作业按契约跑增量，两边说的不是一回事。
        """
        adapter = get_job_adapter(tool)
        logical = _generator.build_logical_schema(
            db,
            ontology_id,
            database_prefix=database_prefix,
            database_overrides=database_overrides,
            table_overrides=table_overrides,
        )
        # 逻辑计划的提示（缺主键、N:N、粒度冲突…）原样带出但**单独放**：
        # 它们说的是目标表结构，多数不妨碍搬运，混进 unsupported 会严重误导。
        plan = JobPlan(schema_notes=list(logical.unsupported))

        source_refs = _generator._source_refs(db, ontology_id)
        source_urns = self._source_urns(db, ontology_id)
        field_refs = _generator._field_refs(db, ontology_id)
        selected = set(selected_targets) if selected_targets else None

        jobs: list[JobSpec] = []
        for semantic_table in logical.schema.tables:
            entity = semantic_table.source_name
            if target_ods_database:
                requested_keys = tuple((target_primary_keys or {}).get(entity) or ())
                constraints = tuple(
                    c for c in semantic_table.constraints if c.kind != "primary_key"
                )
                if requested_keys:
                    constraints += (LogicalConstraint(kind="primary_key", columns=requested_keys),)
                table = replace(
                    semantic_table,
                    database=target_ods_database,
                    name=(target_ods_tables or {}).get(entity) or semantic_table.source_name,
                    layer="ods",
                    constraints=constraints,
                )
            else:
                table = semantic_table
            if selected is not None and entity not in selected:
                continue
            if semantic_table.layer == "ads":
                # 与 M3 的 ETL 口径一致：ADS 由 MetricSpec 算出，不是字段搬运。
                plan.note(semantic_table.qualified_name, "ADS 指标表由 MetricSpec 生成，不产搬运作业")
                continue

            source_name = (source_physical_tables or {}).get(entity) or source_refs.get(entity)
            if not source_name:
                # 两种成因合流于此：压根没有 source_ref，或是人工建模对象（``manual:`` 引用）。
                # 后者此前会漏到下面的平台检查，报「source_ref 未带数据平台信息」——
                # 那句话把「本来就没有源表」误诊成「URN 写得不全」。
                plan.note(table.qualified_name, NO_PHYSICAL_SOURCE_NOTE)
                continue
            urn = source_urns.get(entity)
            platform = (source_platforms or {}).get(entity) or source_platform_of(urn)
            if not platform:
                plan.note(
                    table.qualified_name,
                    f"source_ref 未带数据平台信息（{urn or source_name}），无法选择连接器",
                )
                continue

            # 显式覆盖 > 该表契约 > full。覆盖是「这一次这么跑」，不写回契约。
            mode = (load_strategy or table.load_strategy or "full").strip().lower()
            # 可搬性门禁：runner 通道按执行侧 capabilities，docker 通道按工具适配器。
            if runner_capabilities is not None:
                reason = _runner_reject(runner_capabilities, mode, platform, engine)
                if reason:
                    plan.note(table.qualified_name, reason)
                    continue
            else:
                if not adapter.supports(mode):
                    plan.note(table.qualified_name, f"{adapter.name} 不支持装载方式 {mode}")
                    continue
                if mode == "cdc" and not adapter.supports_cdc_from(platform):
                    # 不静默退回全量：CDC 退成全量会改变数据语义，必须让人看见。
                    plan.note(
                        table.qualified_name,
                        f"契约要求 CDC，但 {adapter.name} 无 {platform} 的 CDC 连接器",
                    )
                    continue
            if mode == "incremental" and not table.partition_key:
                # 与 M3 的 warning 同义：允许生成，但必须显式提示可能重复。
                plan.note(
                    table.qualified_name,
                    "增量装载但契约未配分区键，作业将无水位谓词，可能产生重复",
                )

            column_map = {
                **field_refs.get(entity, {}),
                **(source_mappings or {}).get(entity, {}),
            }
            src_db, src_table = _split_qualified(source_name)
            jobs.append(
                JobSpec(
                    name=_task_name(table.layer, table.name),
                    source=JobEndpoint(
                        alias=source_alias,
                        platform=platform,
                        database=src_db,
                        table=src_table,
                    ),
                    target=JobEndpoint(
                        alias=target_alias,
                        platform=engine,
                        database=table.database,
                        table=table.name,
                    ),
                    # 列映射与 M3 的 SELECT 完全同口径：有 source_field_ref 用它，否则同名。
                    columns=tuple(
                        ColumnMapping(source=column_map.get(c.name) or c.name, target=c.name)
                        for c in table.columns
                    ),
                    mode=mode,
                    partition_key=table.partition_key,
                    incremental_column=(incremental_columns or {}).get(entity),
                    initial_watermark=(initial_watermarks or {}).get(entity),
                    delete_policy=(delete_policies or {}).get(entity) or "ignore",
                    layer=table.layer,
                    source_urn=urn,
                    entity_name=entity,
                    # 带类型目标表：Flink SQL 编译要列类型，planner 此刻正持有它，顺手带上
                    # （避免下游 build_flink_etl_input 逐实体重建 schema，见统一执行重构 B）。
                    target_table=table,
                )
            )

        # 稳定排序 + 最终唯一性：同一本体重复生成必须逐字节一致；同一个 Airflow task_id
        # 或目标表也只能有一个执行者，否则 DAG 无法 import 或会并发写同一张表。
        plan.jobs = _deduplicate_jobs(jobs)
        return plan

    @staticmethod
    def _source_urns(db: Session, ontology_id: str) -> dict[str, str]:
        """实体名 → 原始 source_ref（URN 原样保留，供血缘上报直接用作上游标识）。"""
        from app.models import ObjectType

        return {
            obj.name: obj.source_ref
            for obj in db.query(ObjectType)
            .filter(ObjectType.ontology_id == ontology_id)
            .all()
            if has_physical_source(obj.source_ref)
        }

    def render(self, plan: JobPlan, *, tool: str | None = None) -> dict[str, dict]:
        """JobPlan → ``{作业名: 工具配置}``。工具特定逻辑全在 Adapter 里。"""
        adapter = get_job_adapter(tool)
        return {job.name: adapter.render(job) for job in plan.jobs}


# 与 warehouse_generator 同样的模块级单例用法，便于 runner/executor 直接引用。
job_planner = JobPlanner()


__all__ = [
    "DEFAULT_SOURCE_ALIAS",
    "DEFAULT_TARGET_ALIAS",
    "JobPlanningError",
    "JobPlanner",
    "job_planner",
]
