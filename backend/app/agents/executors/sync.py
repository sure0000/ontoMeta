"""① 同步作业 Executor —— 只**搬数据**，统一走 Flink SQL on YARN。

物化 = 建结构（DDL），同步 = 搬数据（DML），且物化在先。故这里调
``materialization_runner.run_sync``：只产 Flink SQL 搬运作业（全量走 staging + 原子
切换），**不建业务表**——目标表须已由物化任务建好。回执的 ``tables`` 因而恒为空
（前端拿它判「已物化」，同步填了就会冒充成物化）。

与 transform/metric 同一条执行路径（Flink SQL → BashOperator ``flink run``）。
不再渲染 SeaTunnel/DataX 作业配置——那套多通道已废除。

**凭据不进产物**：生成的 Flink SQL 里只有 `${别名_*}` 占位符，运行期由 Airflow
Connection 解析（见 flink_sql_generator / endpoint_credential_env）。
"""

from __future__ import annotations

from typing import Any

from app.agents.executors.base import Executor
from app.services import flink_params
from app.services.job_planner import DEFAULT_SOURCE_ALIAS


class SyncExecutor(Executor):
    kind = "sync"

    def _plan(self, spec: dict[str, Any]) -> dict[str, Any]:
        """搬运计划的静态描述（不连库、不生成完整 SQL）：给 dry_run 与「仅产出」兜底用。

        真正的 Flink SQL 由 execute 经 materialization_runner 产出（那里有 DB 与本体）。
        本描述只回答「要把谁搬到哪、什么装载方式」，够界面展示与链上游对账。
        """
        preservation = spec.get("preservation") or {}
        return {
            "source": spec.get("source"),
            "target": spec.get("target"),
            "object_type": spec.get("object_type"),
            "mode": spec.get("mode") or "full",
            "engine": spec.get("engine") or "hive",
            # 凭据不入产物：只放源库连接别名（= Airflow conn_id）。
            "source_ref_alias": spec.get("source_ref_alias") or DEFAULT_SOURCE_ALIAS,
            "preserved": bool(preservation.get("preserve")),
            "preservation_reason": preservation.get("reason"),
        }

    def dry_run(self, spec: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        plan = self._plan(spec)
        return {
            "action": "flink_sql_move",
            "source": plan["source"],
            "target": plan["target"],
            "mode": plan["mode"],
            "engine": plan["engine"],
            "preserved": plan["preserved"],
            "preservation_reason": plan["preservation_reason"],
            "side_effects": "无（dry-run 不生成/不提交作业）",
        }

    def execute(self, spec: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        # 未配 target_datasource，退回「仅产出」（与 transform/metric 同逻辑）。
        target_datasource_id = spec.get("target_datasource_id") or context.get(
            "target_datasource_id"
        )
        if not target_datasource_id:
            return {
                **self._plan(spec),
                "handoff": "flink_sql",
                "note": "未配置 target_datasource_id，ontoMeta 只给出搬运计划，不执行（链上游会传入此字段）",
            }

        # 进链执行：sync = 只搬数据（不建表）。走 materialization_runner 的 Flink SQL 通道，
        # 传 selected_targets=[对象名] 只搬那一个对象。回执带 dag_id，可被
        # pipeline_compiler 串进链 DAG。
        object_type = spec.get("object_type")
        if not object_type:
            return {
                **self._plan(spec),
                "handoff": "flink_sql",
                "note": "spec 缺 object_type，无法定位要搬的对象，退回仅产出",
            }

        try:
            from app.services import materialization_runner
        except ImportError as exc:
            return {
                **self._plan(spec),
                "handoff": "import_error",
                "note": f"搬运模块导入失败：{exc}，退回仅产出",
            }

        ontology_id = spec.get("ontology_id")
        if not ontology_id:
            raise ValueError("Spec 缺 ontology_id")
        engine = spec.get("engine") or "hive"

        from app.database import SessionLocal

        with SessionLocal() as db:
            try:
                receipt = materialization_runner.run_sync(
                    db,
                    ontology_id,
                    target_datasource_id=target_datasource_id,
                    engine=engine,
                    database_prefix=spec.get("database_prefix"),
                    load_strategy=spec.get("mode"),
                    # 只搬这一个对象（按实体名裁剪）
                    selected_targets=[object_type],
                    artifact_id=context.get("artifact_id"),
                    # 这条同步自己的 Flink 提交参数（并行度/队列/提交目标/checkpoint/
                    # 额外 -D）：Spec 优先、context 兜底，留空的项跟随设置页默认。
                    flink_task_params=flink_params.from_spec(spec, context),
                )
            except materialization_runner.MaterializationError as exc:
                # 未配 Airflow / Flink / 投递失败 / 无可搬对象：退回仅产出，不静默假装执行了。
                return {
                    **self._plan(spec),
                    "handoff": "flink_sql",
                    "note": f"搬运未执行（{exc}），退回仅产出",
                }

        # 关键源保全（STG 原始副本）：判定仍在（drafter 产出），但 Flink 搬运路径尚未
        # 实现「额外产一份贴源 STG 作业」。不静默吞掉——如实在回执里标注为未落地，
        # 供人看见（真要保全需另在 Flink 路径产一个 full→STG 的搬运，属后续工作）。
        if (spec.get("preservation") or {}).get("preserve"):
            receipt = {
                **receipt,
                "preservation_pending": True,
                "preservation_reason": spec["preservation"].get("reason"),
                "preservation_note": (
                    "关键源保全（STG 原始副本）尚未在 Flink 搬运路径实现，本次未产出 STG 副本"
                ),
            }
        return receipt
