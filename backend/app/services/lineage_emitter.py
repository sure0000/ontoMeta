"""物化血缘兜底上报（M11）：一次物化跑完，DataHub 里能查到 源表 → 目标表 的血缘。

**血缘的主路径在执行侧**：Airflow 的 DataHub 插件按任务的 inlets/outlets 自动上报
DataFlow/DataJob 与 Dataset 级血缘（见 `airflow_dag_builder.py`，outlets 已注入真实
目标表 URN）。本模块是**兜底**：插件版本不匹配、Airflow 未接入、或想在提交后立刻补
一条血缘时，由 ontoMeta 直接向 DataHub 发。

**两条路径产同一份 URN**：上游用本体的 ``ObjectType.source_ref``（本就是 DataHub URN），
下游用 ``datahub.build_dataset_urn(平台, 库.表, fabric)``——DAG outlets 与本模块走的是
同一个构造函数，故重复上报是幂等的（DataHub 对已存在的边不重复建）。

**与 M7 回写同构的安全姿态**：
- ``preview`` / ``apply`` 分离——先看清楚要连哪些边，再决定是否落。
- 单条失败不中断整体，逐条记录便于重放。
- 复用 M7 的 GraphQL 通道（``connectors/datahub``），不新开写入口子。

**字段级血缘（fineGrainedLineages）**：列映射事实源（``JobSpec.columns``）已经有了，
本模块把它算进计划并在 ``to_dict`` 里暴露供审计；但 DataHub 的 ``updateLineage``
GraphQL **只接受表级边**，字段级需 aspect 级 emitter 且受目标 DataHub 版本支持度制约
（⚠ 需实施前核实），故当前**只上报表级**、不臆造一个不存在的 GraphQL 形状。
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.connectors import datahub as dh
from app.services.job_planner import JobPlanner
from app.services.settings_service import SettingsService

# DataHub 环境标（PROD/DEV/…）的兵底默认。实际取值优先读设置页（datahub_settings.fabric），
# 未配置时才用它——目标表 URN 的 fabric 属于部署环境，不在深层硬编（见 build_dataset_urn 说明）。
DEFAULT_FABRIC = "PROD"

_job_planner = JobPlanner()
_settings = SettingsService()


@dataclass
class LineageEdge:
    """一条待上报的血缘边：源表 → 目标表。

    ``columns`` 是字段级映射（源列 → 目标列），当前仅供审计展示，不随表级边上报
    （见模块 docstring）。``skipped_reason`` 非空表示此边不落（如源表无 URN）。
    """

    source_urn: str
    target_urn: str
    target_table: str
    columns: list[dict[str, str]] = field(default_factory=list)
    skipped_reason: str | None = None

    @property
    def will_apply(self) -> bool:
        return self.skipped_reason is None


@dataclass
class LineagePlan:
    ontology_id: str
    edges: list[LineageEdge] = field(default_factory=list)
    blocked_reason: str | None = None

    @property
    def applicable(self) -> list[LineageEdge]:
        return [e for e in self.edges if e.will_apply]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ontology_id": self.ontology_id,
            "blocked_reason": self.blocked_reason,
            "total": len(self.edges),
            "applicable": len(self.applicable),
            "skipped": len(self.edges) - len(self.applicable),
            # 字段级映射条数：暴露供审计，表明「有多少列映射可供未来字段级血缘」。
            "column_mappings": sum(len(e.columns) for e in self.applicable),
            "edges": [asdict(e) for e in self.edges],
        }


class LineageEmitter:
    def build_plan(
        self,
        db: Session,
        ontology_id: str,
        *,
        engine: str,
        fabric: str | None = None,
        database_prefix: str | None = None,
        database_overrides: dict[str, str] | None = None,
        table_overrides: dict[str, str] | None = None,
        selected_targets: list[str] | None = None,
    ) -> LineagePlan:
        """列出将要上报的血缘边。纯读，不触碰 DataHub。

        入参与物化一致（同一 ``JobPlanner`` 事实源）——保证上报的边与实际搬运的作业
        一一对应，不多连、不漏连。``fabric`` 缺省取自设置页（datahub_settings.fabric）。
        """
        fabric = fabric or _settings.get_datahub_runtime(db).fabric

        job_plan = _job_planner.build(
            db,
            ontology_id,
            engine=engine,
            database_prefix=database_prefix,
            database_overrides=database_overrides,
            table_overrides=table_overrides,
            selected_targets=selected_targets,
        )
        plan = LineagePlan(ontology_id=ontology_id)
        for job in job_plan.jobs:
            target_urn = dh.build_dataset_urn(
                job.target.platform, job.target.qualified, fabric
            )
            columns = [{"source": c.source, "target": c.target} for c in job.columns]
            if not job.source_urn:
                # 理论上 JobPlanner 已滤掉无 source_ref 的表，这里是防御：
                # 无上游 URN 就连不成边，显式跳过而非发一条断头边。
                plan.edges.append(
                    LineageEdge(
                        source_urn="",
                        target_urn=target_urn,
                        target_table=job.target.qualified,
                        columns=columns,
                        skipped_reason="作业无源表 URN，无法构成血缘边",
                    )
                )
                continue
            plan.edges.append(
                LineageEdge(
                    source_urn=job.source_urn,
                    target_urn=target_urn,
                    target_table=job.target.qualified,
                    columns=columns,
                )
            )
        return plan

    async def apply(
        self,
        db: Session,
        ontology_id: str,
        *,
        engine: str,
        fabric: str | None = None,
        database_prefix: str | None = None,
        database_overrides: dict[str, str] | None = None,
        table_overrides: dict[str, str] | None = None,
        selected_targets: list[str] | None = None,
        connector=None,
    ) -> dict[str, Any]:
        """上报血缘。失败逐条记录，不因单条失败中断整体。"""
        plan = self.build_plan(
            db,
            ontology_id,
            engine=engine,
            fabric=fabric,
            database_prefix=database_prefix,
            database_overrides=database_overrides,
            table_overrides=table_overrides,
            selected_targets=selected_targets,
        )
        if plan.blocked_reason:
            return {**plan.to_dict(), "applied": 0, "failed": 0, "errors": []}

        owns_connector = connector is None
        if owns_connector:
            connector = dh.DataHubConnector(_settings.get_datahub_runtime(db))

        applied = 0
        errors: list[dict[str, str]] = []
        try:
            for edge in plan.applicable:
                try:
                    await dh.add_lineage_edge(
                        connector, edge.source_urn, edge.target_urn
                    )
                    applied += 1
                except dh.DataHubWriteError as exc:
                    errors.append(
                        {
                            "target": edge.target_table,
                            "source_urn": edge.source_urn,
                            "error": str(exc.cause),
                        }
                    )
        finally:
            if owns_connector:
                await connector.aclose()

        return {
            **plan.to_dict(),
            "applied": applied,
            "failed": len(errors),
            "errors": errors,
        }

    def apply_sync(self, db: Session, ontology_id: str, **kwargs) -> dict[str, Any]:
        """同步入口，供 FastAPI 同步路由调用。"""
        return asyncio.run(self.apply(db, ontology_id, **kwargs))
