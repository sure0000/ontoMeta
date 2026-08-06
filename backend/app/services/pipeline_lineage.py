"""P3-3：任务链级血缘上报 DataHub。

**链级血缘**：串联链的各步产出表（物化 → 清洗 → 聚合），在 DataHub 展示完整数据流。
比照 M11 物化血缘的 preview/apply 分离、逐条记录失败、幂等姿态。

**血缘构造**：从 TaskPipeline 的各步制品回执里提取 target_table，按 step_index 串成
上一步目标 → 本步目标 的边。各步的 target_table 格式是 `database.table`，用
`build_dataset_urn(platform, database.table, fabric)` 构造 URN。

**与单步血缘的关系**：
- 单步血缘（M11）：源系统表 → 目标仓库表（由 Airflow 插件或 lineage_emitter 上报）
- 链级血缘（P3-3）：仓库表 → 仓库表（物化的 DWD → transform 的 DIM → metric 的 ADS）
- 两者不冲突，都是 Dataset 级血缘，在 DataHub 里合成完整图谱

**不做 DataFlow/DataJob**：DataHub 的 DataFlow 建模的是「一个调度单元」（如一条 DAG），
而链已经编译成一条周期 DAG 了，该 DAG 的元数据由 Airflow 插件上报。本模块只补链内各步
之间的表级血缘边，不重复建 DataFlow——重复会产生两个同名 DataFlow，令人困惑。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from sqlalchemy.orm import Session

from app.connectors.datahub import DataHubConnector, DataHubWriteError, build_dataset_urn
from app.models.agent import GovernanceArtifact, GovernanceTaskPipeline
from app.services.settings_service import SettingsService
from app.services.task_pipeline import TaskPipelineService

_settings = SettingsService()
_pipeline_service = TaskPipelineService()


class PipelineLineageEmitter:
    """链级血缘上报器（P3-3）。"""

    def preview(self, db: Session, pipeline_id: str) -> dict[str, Any]:
        """预览：从链的各步回执提取目标表，构造血缘边（不上报）。

        Returns:
            {
                "pipeline_id": str,
                "pipeline_name": str,
                "edges": [{"source_urn": str, "target_urn": str, "source_step": int, "target_step": int}],
                "skipped": [{"step_index": int, "reason": str}],
                "ready": bool,  # 所有步骤都有目标表，可上报
            }

        Raises:
            LookupError: 链不存在
        """
        pipeline = _pipeline_service.require(db, pipeline_id)
        detail = _pipeline_service.detail(db, pipeline_id)
        steps = detail["steps"]

        # 提取各步的目标表（从制品回执或 spec）
        step_tables: list[tuple[int, str] | None] = []
        skipped = []
        for step in steps:
            artifact_id = step["artifact_id"]
            if not artifact_id:
                skipped.append({"step_index": step["step_index"], "reason": "尚未起草"})
                step_tables.append(None)
                continue

            artifact = db.get(GovernanceArtifact, artifact_id)
            if not artifact or not artifact.execution_receipt_json:
                skipped.append({"step_index": step["step_index"], "reason": "尚未执行"})
                step_tables.append(None)
                continue

            try:
                receipt = json.loads(artifact.execution_receipt_json)
                spec = json.loads(artifact.spec_json or "{}")
            except (TypeError, ValueError):
                skipped.append({"step_index": step["step_index"], "reason": "回执解析失败"})
                step_tables.append(None)
                continue

            # 优先从回执读，fallback 到 spec
            target_table = receipt.get("target_table") or spec.get("target_table")
            if not target_table:
                skipped.append({"step_index": step["step_index"], "reason": "回执里没有 target_table"})
                step_tables.append(None)
                continue

            # platform 取自制品的 engine（各 executor 的 spec 都带 engine，缺省 hive）
            engine = receipt.get("engine") or spec.get("engine") or "hive"
            step_tables.append((step["step_index"], target_table, engine))

        # 构造边：step[i] 的目标 → step[i+1] 的目标
        edges = []
        fabric = _settings.get_datahub_runtime(db).fabric

        for i in range(len(step_tables) - 1):
            curr = step_tables[i]
            next_ = step_tables[i + 1]
            if curr is None or next_ is None:
                continue
            curr_idx, curr_table, curr_engine = curr
            next_idx, next_table, next_engine = next_

            source_urn = build_dataset_urn(curr_engine, curr_table, fabric)
            target_urn = build_dataset_urn(next_engine, next_table, fabric)
            edges.append({
                "source_urn": source_urn,
                "target_urn": target_urn,
                "source_step": curr_idx,
                "target_step": next_idx,
            })

        return {
            "pipeline_id": pipeline_id,
            "pipeline_name": pipeline.name,
            "edges": edges,
            "skipped": skipped,
            "ready": len(edges) > 0 and len(skipped) == 0,
        }

    async def apply(
        self,
        db: Session,
        pipeline_id: str,
        *,
        connector: DataHubConnector | None = None,
    ) -> dict[str, Any]:
        """应用：上报链级血缘到 DataHub。

        Returns:
            {
                ...preview 的全部字段,
                "applied": int,  # 成功上报的边数
                "failed": int,
                "errors": [{"source_urn": str, "target_urn": str, "error": str}],
            }
        """
        plan = self.preview(db, pipeline_id)
        if not plan["edges"]:
            return {**plan, "applied": 0, "failed": 0, "errors": []}

        from app.connectors import datahub as dh

        owns_connector = connector is None
        if owns_connector:
            dh_rt = _settings.get_datahub_runtime(db)
            connector = dh.DataHubConnector(
                endpoint=dh_rt.endpoint,
                token=dh_rt.token,
                timeout=dh_rt.timeout or 30,
            )

        applied = 0
        errors = []
        try:
            for edge in plan["edges"]:
                try:
                    await dh.add_lineage_edge(
                        connector, edge["source_urn"], edge["target_urn"]
                    )
                    applied += 1
                except DataHubWriteError as exc:
                    errors.append({
                        "source_urn": edge["source_urn"],
                        "target_urn": edge["target_urn"],
                        "error": str(exc.cause),
                    })
        finally:
            if owns_connector:
                await connector.aclose()

        return {
            **plan,
            "applied": applied,
            "failed": len(errors),
            "errors": errors,
        }

    def apply_sync(self, db: Session, pipeline_id: str, **kwargs) -> dict[str, Any]:
        """同步入口，供 FastAPI 同步路由调用。"""
        return asyncio.run(self.apply(db, pipeline_id, **kwargs))
