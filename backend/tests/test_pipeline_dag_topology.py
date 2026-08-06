"""P3-2：链支持扇出/汇聚（DAG 形态）测试。"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from app.database import SessionLocal
from app.models.agent import ArtifactStatus, GovernanceArtifact, GovernanceTaskPipeline, GovernanceTaskPipelineStep
from app.services.pipeline_compiler import PipelineCompileError, _validate_dag_topology, _render_chain_dag


def test_validate_dag_topology_linear_ok():
    """线性链（默认 depends_on）拓扑排序通过。"""
    steps = [
        {"step_index": 0, "kind": "materialize"},
        {"step_index": 1, "kind": "transform"},
        {"step_index": 2, "kind": "metric"},
    ]
    # 不抛异常即通过
    _validate_dag_topology(steps)


def test_validate_dag_topology_fanout_ok():
    """扇出（一个上游分叉到多个下游）拓扑排序通过。"""
    steps = [
        {"step_index": 0, "kind": "materialize", "depends_on": []},
        {"step_index": 1, "kind": "transform", "depends_on": [0]},
        {"step_index": 2, "kind": "metric", "depends_on": [0]},  # 也依赖步骤 0（扇出）
    ]
    _validate_dag_topology(steps)


def test_validate_dag_topology_merge_ok():
    """汇聚（多个上游汇到一个下游）拓扑排序通过。"""
    steps = [
        {"step_index": 0, "kind": "materialize", "depends_on": []},
        {"step_index": 1, "kind": "transform", "depends_on": []},
        {"step_index": 2, "kind": "metric", "depends_on": [0, 1]},  # 依赖两个上游（汇聚）
    ]
    _validate_dag_topology(steps)


def test_validate_dag_topology_cycle_fails():
    """循环依赖拓扑排序失败。"""
    steps = [
        {"step_index": 0, "kind": "materialize", "depends_on": []},
        {"step_index": 1, "kind": "transform", "depends_on": [0]},
        {"step_index": 2, "kind": "metric", "depends_on": [1]},
        {"step_index": 3, "kind": "transform", "depends_on": [2]},
        {"step_index": 4, "kind": "metric", "depends_on": [3, 1]},  # 4 → 3 → 2 → 1，但 4 也依赖 1，形成环
    ]
    # 实际上这个例子不成环（DAG 形态下多个路径可达不算环）。改成真正的环：
    steps = [
        {"step_index": 0, "kind": "materialize", "depends_on": [2]},  # 0 依赖 2
        {"step_index": 1, "kind": "transform", "depends_on": [0]},   # 1 依赖 0
        {"step_index": 2, "kind": "metric", "depends_on": [1]},      # 2 依赖 1 → 0 → 2，成环
    ]
    with pytest.raises(PipelineCompileError, match="循环依赖"):
        _validate_dag_topology(steps)


def test_validate_dag_topology_missing_upstream_fails():
    """依赖的上游步骤不存在，拓扑排序失败。"""
    steps = [
        {"step_index": 0, "kind": "materialize", "depends_on": []},
        {"step_index": 1, "kind": "transform", "depends_on": [99]},  # 步骤 99 不存在
    ]
    with pytest.raises(PipelineCompileError, match="不存在"):
        _validate_dag_topology(steps)


def test_render_chain_dag_with_explicit_depends_on():
    """渲染 DAG 时按 depends_on 串联（而非纯线性）。"""
    steps = [
        {"step_index": 0, "kind": "materialize", "dag_ids": ["dag_m"], "depends_on": []},
        {"step_index": 1, "kind": "transform", "dag_ids": ["dag_t1"], "depends_on": [0]},
        {"step_index": 2, "kind": "transform", "dag_ids": ["dag_t2"], "depends_on": [0]},  # 扇出
        {"step_index": 3, "kind": "metric", "dag_ids": ["dag_a"], "depends_on": [1, 2]},  # 汇聚
    ]
    source = _render_chain_dag("test_fanout_dag", "0 2 * * *", steps, "扇出汇聚链")

    # 验证步骤 0 的触发器被步骤 1、2 依赖（扇出）
    assert "run_step0_materialize_dag0 >> run_step1_transform_dag0" in source
    assert "run_step0_materialize_dag0 >> run_step2_transform_dag0" in source

    # 验证步骤 3 依赖步骤 1、2（汇聚）
    assert "run_step1_transform_dag0 >> run_step3_metric_dag0" in source
    assert "run_step2_transform_dag0 >> run_step3_metric_dag0" in source


def test_compile_pipeline_with_dag_topology(monkeypatch):
    """端到端：编译带 DAG 形态的链（扇出）。"""
    import tempfile
    from unittest.mock import MagicMock

    from app.services import pipeline_compiler
    from app.services.pipeline_compiler import compile_pipeline

    with SessionLocal() as db:
        with tempfile.TemporaryDirectory() as tmpdir:
            # mock Airflow 配置指向临时目录
            rt = MagicMock(available=True, dags_dir=tmpdir)
            monkeypatch.setattr(
                pipeline_compiler._settings, "get_airflow_runtime", lambda db: rt
            )

            now = datetime.now(timezone.utc)
            pipeline = GovernanceTaskPipeline(
                name="test-dag-chain", ontology_id="onto-x", schedule_cron="0 2 * * *"
            )
            db.add(pipeline)
            db.flush()

            # 步骤 0：materialize
            art0 = GovernanceArtifact(
                kind="materialize", name="物化", ontology_id="onto-x",
                status=ArtifactStatus.CONFIRMED.value,
                spec_json=json.dumps({"engine": "hive"}),
                execution_receipt_json=json.dumps({"batches": [{"dag_id": "dag_m"}]}),
                confirmed_at=now, updated_at=now,
            )
            db.add(art0)
            db.flush()
            db.add(GovernanceTaskPipelineStep(
                pipeline_id=pipeline.id, step_index=0, kind="materialize",
                intent="物化", artifact_id=art0.id,
                depends_on_json=None,  # 线性默认
            ))

            # 步骤 1：transform（依赖步骤 0）
            art1 = GovernanceArtifact(
                kind="transform", name="清洗1", ontology_id="onto-x",
                status=ArtifactStatus.CONFIRMED.value,
                spec_json=json.dumps({"engine": "hive"}),
                execution_receipt_json=json.dumps({"dag_id": "dag_t1"}),
                confirmed_at=now, updated_at=now,
            )
            db.add(art1)
            db.flush()
            db.add(GovernanceTaskPipelineStep(
                pipeline_id=pipeline.id, step_index=1, kind="transform",
                intent="清洗1", artifact_id=art1.id,
                depends_on_json=json.dumps([0]),
            ))

            # 步骤 2：transform（也依赖步骤 0，扇出）
            art2 = GovernanceArtifact(
                kind="transform", name="清洗2", ontology_id="onto-x",
                status=ArtifactStatus.CONFIRMED.value,
                spec_json=json.dumps({"engine": "hive"}),
                execution_receipt_json=json.dumps({"dag_id": "dag_t2"}),
                confirmed_at=now, updated_at=now,
            )
            db.add(art2)
            db.flush()
            db.add(GovernanceTaskPipelineStep(
                pipeline_id=pipeline.id, step_index=2, kind="transform",
                intent="清洗2", artifact_id=art2.id,
                depends_on_json=json.dumps([0]),  # 扇出
            ))

            db.commit()
            pid = pipeline.id

            result = compile_pipeline(db, pid)
            assert result["compiled_dag_id"]
            assert len(result["steps"]) == 3

            # 验证 DAG 文件内容有扇出依赖
            import os
            dag_path = result["dag_path"]
            assert os.path.exists(dag_path)
            with open(dag_path, encoding="utf-8") as f:
                source = f.read()
            # 步骤 0 的触发器被步骤 1、2 都依赖
            assert "run_step0_materialize_dag0 >> run_step1_transform_dag0" in source
            assert "run_step0_materialize_dag0 >> run_step2_transform_dag0" in source
