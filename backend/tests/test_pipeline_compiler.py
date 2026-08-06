"""P2-2/P2-3：任务链编译器 + 编译 API 端点测试。

钉住编译门槛：所有步骤已确认、已执行、spec 未变更才可编译；缺 schedule_cron 拒；
未确认/未执行/spec 变更各自报清楚。以及 DAG 生成的结构正确、幂等。
"""

from __future__ import annotations

import ast
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.database import SessionLocal
from app.models.agent import (
    ArtifactStatus,
    GovernanceArtifact,
    GovernanceTaskPipeline,
    GovernanceTaskPipelineStep,
)
from app.services.pipeline_compiler import (
    PipelineCompileError,
    _chain_dag_id,
    _render_chain_dag,
    compile_pipeline,
)


# ---------- 纯函数：DAG id 与渲染 ----------


def test_chain_dag_id_is_stable_and_scoped():
    assert _chain_dag_id("abc-def-123-456").startswith("ontometa_chain_")
    # 同一 id 反复算一致
    assert _chain_dag_id("x-y-z") == _chain_dag_id("x-y-z")


def test_render_chain_dag_uses_trigger_operator_and_compiles():
    steps = [
        {"step_index": 0, "kind": "materialize", "dag_ids": ["m0", "m1"]},
        {"step_index": 1, "kind": "transform", "dag_ids": ["t0"]},
        {"step_index": 2, "kind": "metric", "dag_ids": ["a0"]},
    ]
    src = _render_chain_dag("ontometa_chain_x", "0 2 * * *", steps, "物化→清洗→聚合")
    # 合法 Python
    ast.parse(src)
    # 用主动触发而非 sensor（execution_date 不对齐问题）
    assert "TriggerDagRunOperator" in src
    assert "ExternalTaskSensor" not in src
    assert "wait_for_completion=True" in src
    # materialize 两个批次各一个触发任务
    assert "run_step0_materialize_dag0" in src
    assert "run_step0_materialize_dag1" in src
    # 串联：step0 的触发器 >> step1 的触发器
    assert "run_step0_materialize_dag0 >> run_step1_transform_dag0" in src
    assert "run_step1_transform_dag0 >> run_step2_metric_dag0" in src


def test_render_chain_dag_is_idempotent():
    steps = [{"step_index": 0, "kind": "transform", "dag_ids": ["t0"]}]
    a = _render_chain_dag("c", "0 2 * * *", steps, "链")
    b = _render_chain_dag("c", "0 2 * * *", steps, "链")
    assert a == b


# ---------- 编译门槛 ----------


def _mk_pipeline(schedule_cron: str | None = "0 2 * * *") -> tuple[str, list[str]]:
    """建一条 2 步链（materialize → transform），返回 (pipeline_id, [step_artifact_ids])。

    制品默认建成 confirmed + 有回执 + 未变更（可编译状态）。
    """
    with SessionLocal() as db:
        pipeline = GovernanceTaskPipeline(
            name="test-chain", ontology_id="onto-x", schedule_cron=schedule_cron
        )
        db.add(pipeline)
        db.flush()

        artifact_ids = []
        now = datetime.now(timezone.utc)
        for i, (kind, receipt) in enumerate([
            ("materialize", {"batches": [{"dag_id": "ontometa_materialize_ox__c0"}]}),
            ("transform", {"dag_id": "ontometa_flink_tx"}),
        ]):
            art = GovernanceArtifact(
                kind=kind,
                name=f"step-{i}",
                ontology_id="onto-x",
                status=ArtifactStatus.CONFIRMED.value,
                spec_json=json.dumps({"ontology_id": "onto-x"}),
                execution_receipt_json=json.dumps(receipt),
                confirmed_at=now,
            )
            db.add(art)
            db.flush()
            # updated_at <= confirmed_at（未变更）
            art.updated_at = now - timedelta(seconds=1)
            db.add(GovernanceTaskPipelineStep(
                pipeline_id=pipeline.id, step_index=i, kind=kind,
                intent=f"step {i}", artifact_id=art.id,
            ))
            artifact_ids.append(art.id)
        db.commit()
        return pipeline.id, artifact_ids


@pytest.fixture
def airflow_dir(tmp_path, monkeypatch):
    """把编译器的 Airflow 设置指向 tmp 目录且标 available。"""
    from app.services import pipeline_compiler
    from unittest.mock import MagicMock

    rt = MagicMock(available=True, dags_dir=str(tmp_path / "dags"))
    monkeypatch.setattr(
        pipeline_compiler._settings, "get_airflow_runtime", lambda db: rt
    )
    return tmp_path


def test_compile_rejects_without_schedule_cron(airflow_dir):
    pid, _ = _mk_pipeline(schedule_cron=None)
    with SessionLocal() as db:
        with pytest.raises(PipelineCompileError) as exc:
            compile_pipeline(db, pid)
    assert "schedule_cron" in str(exc.value)


def test_compile_rejects_unconfirmed_step(airflow_dir):
    pid, art_ids = _mk_pipeline()
    with SessionLocal() as db:
        art = db.get(GovernanceArtifact, art_ids[1])
        art.status = ArtifactStatus.DRAFTED.value
        db.commit()
        with pytest.raises(PipelineCompileError) as exc:
            compile_pipeline(db, pid)
    assert "尚未确认" in str(exc.value)


def test_compile_rejects_unexecuted_step(airflow_dir):
    pid, art_ids = _mk_pipeline()
    with SessionLocal() as db:
        art = db.get(GovernanceArtifact, art_ids[0])
        art.execution_receipt_json = None
        db.commit()
        with pytest.raises(PipelineCompileError) as exc:
            compile_pipeline(db, pid)
    assert "尚未执行" in str(exc.value)


def test_compile_rejects_spec_changed_after_confirm(airflow_dir):
    pid, art_ids = _mk_pipeline()
    with SessionLocal() as db:
        art = db.get(GovernanceArtifact, art_ids[1])
        # updated_at 晚于 confirmed_at → spec 确认后又改了
        art.updated_at = art.confirmed_at + timedelta(minutes=5)
        db.commit()
        with pytest.raises(PipelineCompileError) as exc:
            compile_pipeline(db, pid)
    assert "变更" in str(exc.value)


def test_compile_succeeds_and_writes_dag(airflow_dir):
    pid, _ = _mk_pipeline()
    with SessionLocal() as db:
        result = compile_pipeline(db, pid)

    assert result["compiled_dag_id"].startswith("ontometa_chain_")
    assert result["schedule_cron"] == "0 2 * * *"
    # 两步各一个 dag
    assert len(result["steps"]) == 2
    # DAG 文件落盘且是合法 Python
    dag_path = airflow_dir / "dags" / f"{result['compiled_dag_id']}.py"
    assert dag_path.exists()
    ast.parse(dag_path.read_text())
    # spec.json 也落盘
    assert (airflow_dir / "dags" / f"{result['compiled_dag_id']}.json").exists()

    # 链的 compiled 字段已更新
    with SessionLocal() as db:
        pipeline = db.get(GovernanceTaskPipeline, pid)
        assert pipeline.compiled_dag_id == result["compiled_dag_id"]
        assert pipeline.compiled_at is not None


def test_compile_rejects_receipt_without_dag_id(airflow_dir):
    pid, art_ids = _mk_pipeline()
    with SessionLocal() as db:
        art = db.get(GovernanceArtifact, art_ids[1])
        art.execution_receipt_json = json.dumps({"note": "仅产出"})  # 无 dag_id
        db.commit()
        with pytest.raises(PipelineCompileError) as exc:
            compile_pipeline(db, pid)
    assert "dag_id" in str(exc.value)
