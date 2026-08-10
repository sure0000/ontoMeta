"""① 同步作业 Executor —— 对单个对象跑「搬运」，统一走 Flink SQL on YARN。

统一执行架构：sync = 对单对象的搬运，与 materialize/transform/metric 同一条执行路径
（`materialization_runner.run` → Flink SQL → BashOperator `flink run`）。不再渲染
SeaTunnel/DataX 作业配置——那套多通道已废除。

**凭据不进产物**：生成的 Flink SQL 里只有 `${别名_*}` 占位符，运行期由 Airflow
Connection 解析（见 flink_sql_generator / endpoint_credential_env）。
"""

from __future__ import annotations

from typing import Any

from app.agents.executors.base import Executor
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

        # 进链执行：sync = 对单对象搬运。复用 materialization_runner 的 Flink SQL 通道，
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
                receipt = materialization_runner.run(
                    db,
                    ontology_id,
                    target_datasource_id=target_datasource_id,
                    engine=engine,
                    database_prefix=spec.get("database_prefix"),
                    load_strategy=spec.get("mode"),
                    # 只搬这一个对象（按实体名裁剪）
                    selected_targets=[object_type],
                    artifact_id=context.get("artifact_id"),
                )
            except materialization_runner.MaterializationError as exc:
                # 未配 Airflow / Flink / 投递失败：退回仅产出，不静默假装执行了。
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
