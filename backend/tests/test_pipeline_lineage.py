"""P3-3：链级血缘上报 DataHub 测试。"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from app.database import SessionLocal
from app.models.agent import ArtifactStatus, GovernanceArtifact, GovernanceTaskPipeline, GovernanceTaskPipelineStep
from app.services.pipeline_lineage import PipelineLineageEmitter


def _mk_pipeline_with_tables() -> str:
    """建一条 3 步链（materialize → transform → metric），各步已执行且有 target_table。"""
    with SessionLocal() as db:
        pipeline = GovernanceTaskPipeline(
            name="test-lineage-chain", ontology_id="onto-x", schedule_cron="0 2 * * *"
        )
        db.add(pipeline)
        db.flush()

        now = datetime.now(timezone.utc)
        steps_data = [
            ("materialize", "ods.fact_orders", "hive", {"batches": [{"dag_id": "dag_m"}]}),
            ("transform", "dwd.dim_orders", "hive", {"dag_id": "dag_t"}),
            ("metric", "ads.metric_orders_daily", "hive", {"dag_id": "dag_a"}),
        ]

        for i, (kind, target_table, engine, receipt_extra) in enumerate(steps_data):
            art = GovernanceArtifact(
                kind=kind,
                name=f"step-{i}",
                ontology_id="onto-x",
                status=ArtifactStatus.CONFIRMED.value,
                spec_json=json.dumps({"target_table": target_table, "engine": engine}),
                execution_receipt_json=json.dumps({
                    "target_table": target_table,
                    "engine": engine,
                    **receipt_extra,
                }),
                confirmed_at=now,
                updated_at=now,
            )
            db.add(art)
            db.flush()
            db.add(GovernanceTaskPipelineStep(
                pipeline_id=pipeline.id, step_index=i, kind=kind,
                intent=f"step {i}", artifact_id=art.id,
            ))
        db.commit()
        return pipeline.id


def test_preview_extracts_step_tables_and_builds_edges():
    pid = _mk_pipeline_with_tables()
    emitter = PipelineLineageEmitter()

    with SessionLocal() as db:
        plan = emitter.preview(db, pid)

    assert plan["pipeline_id"] == pid
    assert plan["ready"] is True
    assert len(plan["skipped"]) == 0
    # 3 步 → 2 条边（0→1, 1→2）
    assert len(plan["edges"]) == 2
    edge0 = plan["edges"][0]
    assert edge0["source_step"] == 0
    assert edge0["target_step"] == 1
    assert "ods.fact_orders" in edge0["source_urn"]
    assert "dwd.dim_orders" in edge0["target_urn"]

    edge1 = plan["edges"][1]
    assert edge1["source_step"] == 1
    assert edge1["target_step"] == 2
    assert "dwd.dim_orders" in edge1["source_urn"]
    assert "ads.metric_orders_daily" in edge1["target_urn"]


def test_preview_skips_unexecuted_steps():
    with SessionLocal() as db:
        pipeline = GovernanceTaskPipeline(
            name="incomplete", ontology_id="onto-x"
        )
        db.add(pipeline)
        db.flush()

        # 第一步有回执
        art1 = GovernanceArtifact(
            kind="materialize", name="s0", ontology_id="onto-x",
            status=ArtifactStatus.CONFIRMED.value,
            execution_receipt_json=json.dumps({"target_table": "ods.t1"}),
        )
        db.add(art1)
        db.flush()
        db.add(GovernanceTaskPipelineStep(
            pipeline_id=pipeline.id, step_index=0, kind="materialize",
            intent="step 0", artifact_id=art1.id,
        ))

        # 第二步没回执
        art2 = GovernanceArtifact(
            kind="transform", name="s1", ontology_id="onto-x",
            status=ArtifactStatus.DRAFTED.value,
            execution_receipt_json=None,
        )
        db.add(art2)
        db.flush()
        db.add(GovernanceTaskPipelineStep(
            pipeline_id=pipeline.id, step_index=1, kind="transform",
            intent="step 1", artifact_id=art2.id,
        ))

        db.commit()
        pid = pipeline.id

    emitter = PipelineLineageEmitter()
    with SessionLocal() as db:
        plan = emitter.preview(db, pid)

    assert plan["ready"] is False
    assert len(plan["skipped"]) == 1
    assert plan["skipped"][0]["step_index"] == 1
    assert "尚未执行" in plan["skipped"][0]["reason"]
    # 只有一步有表，无法形成边
    assert len(plan["edges"]) == 0


def test_preview_handles_missing_target_table():
    with SessionLocal() as db:
        pipeline = GovernanceTaskPipeline(
            name="no-target", ontology_id="onto-x"
        )
        db.add(pipeline)
        db.flush()

        art = GovernanceArtifact(
            kind="transform", name="s0", ontology_id="onto-x",
            status=ArtifactStatus.CONFIRMED.value,
            execution_receipt_json=json.dumps({"note": "仅产出"}),  # 无 target_table
        )
        db.add(art)
        db.flush()
        db.add(GovernanceTaskPipelineStep(
            pipeline_id=pipeline.id, step_index=0, kind="transform",
            intent="step 0", artifact_id=art.id,
        ))
        db.commit()
        pid = pipeline.id

    emitter = PipelineLineageEmitter()
    with SessionLocal() as db:
        plan = emitter.preview(db, pid)

    assert len(plan["skipped"]) == 1
    assert "target_table" in plan["skipped"][0]["reason"]


@pytest.mark.anyio
async def test_apply_reports_edges_to_datahub(monkeypatch):
    from unittest.mock import AsyncMock, MagicMock

    pid = _mk_pipeline_with_tables()

    # Mock DataHub connector
    mock_connector = MagicMock()
    mock_connector.aclose = AsyncMock()
    mock_add_edge = AsyncMock()

    from app.connectors import datahub as dh
    monkeypatch.setattr(dh, "add_lineage_edge", mock_add_edge)
    monkeypatch.setattr(dh.DataHubConnector, "__init__", lambda self, **kw: None)

    emitter = PipelineLineageEmitter()
    with SessionLocal() as db:
        result = await emitter.apply(db, pid, connector=mock_connector)

    assert result["applied"] == 2
    assert result["failed"] == 0
    assert mock_add_edge.call_count == 2
