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

from sqlalchemy.orm import Session

from app.connectors.datahub import _extract_platform
from app.warehouse.jobs import (
    ColumnMapping,
    JobEndpoint,
    JobPlan,
    JobSpec,
    get_job_adapter,
)
from app.services.warehouse_generator import WarehouseGenerator

# 源/目标连接别名的缺省值。**别名不是凭据**：执行侧按别名解析连接串，
# 沿用 ``agents/drafters/sync.py`` 里 ``source_ref_alias`` 的既有约定。
DEFAULT_SOURCE_ALIAS = "erp_readonly"
DEFAULT_TARGET_ALIAS = "warehouse_default"

_generator = WarehouseGenerator()


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
    ) -> JobPlan:
        """产出搬运作业计划。

        ``engine`` 为目标数仓引擎（决定 sink 连接器）；``database_overrides`` /
        ``table_overrides`` 与物化弹窗同义，保证作业写入的库表与 DDL 建的完全一致。
        ``selected_targets`` 按**本体实体名**裁剪（不是物理表名，故改过表名也不会误裁）。
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
        for table in logical.schema.tables:
            entity = table.source_name
            if selected is not None and entity not in selected:
                continue
            if table.layer == "ads":
                # 与 M3 的 ETL 口径一致：ADS 由 MetricSpec 算出，不是字段搬运。
                plan.note(table.qualified_name, "ADS 指标表由 MetricSpec 生成，不产搬运作业")
                continue

            source_name = source_refs.get(entity)
            if not source_name:
                plan.note(table.qualified_name, "对象无 source_ref，无法定位源表")
                continue
            urn = source_urns.get(entity)
            platform = _extract_platform(urn or "")
            if not platform:
                plan.note(
                    table.qualified_name,
                    f"source_ref 未带数据平台信息（{urn or source_name}），无法选择连接器",
                )
                continue

            mode = (table.load_strategy or "full").strip().lower()
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

            column_map = field_refs.get(entity, {})
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
                    layer=table.layer,
                    source_urn=urn,
                )
            )

        # 稳定排序：同一本体重复生成必须逐字节一致（沿用 M3 的幂等要求）。
        plan.jobs = tuple(sorted(jobs, key=lambda j: (j.layer, j.name)))
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
            if obj.source_ref
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
    "JobPlanner",
    "job_planner",
]
