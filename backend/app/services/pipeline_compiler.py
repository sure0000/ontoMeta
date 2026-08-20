"""P2-2：任务链编译器——把链的各步串成一条 Airflow DAG（含周期调度）。

**编译前提（守住门槛）**：
1. 所有步骤已起草且已确认（status=confirmed）
2. 所有步骤已执行过一次（有回执、spec 已验证可行）
3. spec 未在确认后变更（artifact.updated_at <= artifact.confirmed_at）

**编译产物**：
- 一条 Airflow DAG，串联各步的 DAG（materialize 的批次 DAG / transform·metric 的单 DAG）
- 用 TriggerDagRunOperator(wait_for_completion=True) 主动触发各步 DAG 并等其完成，
  下游等上游本周期成功——上游失败时下游不触发（Airflow 默认 all_success）
- schedule 来自链的 schedule_cron，各步 DAG 本身仍可独立手动触发

**为什么不用 ExternalTaskSensor**：它按相同 execution_date 匹配上游 DAG run，而各步
DAG 是手动触发的、execution_date 与链 DAG 的 cron 周期不对齐，sensor 会永远等不到。
TriggerDagRunOperator 主动触发下游、execution_date 由触发时刻决定，不存在对齐问题。

**不变量**：
- 编译不改制品 spec / receipt，只产新 DAG
- 各步 DAG 仍可独立手动触发（链 DAG 是增强，不是替代）
- spec 变更后 compiled_dag_id 失效，需重编译
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.models.agent import ArtifactStatus, GovernanceArtifact
from app.services.settings_service import SettingsService
from app.services.task_pipeline import TaskPipelineService

_settings = SettingsService()
_pipeline_service = TaskPipelineService()


class PipelineCompileError(RuntimeError):
    """编译失败，面向用户可读。"""


def compile_pipeline(db: Session, pipeline_id: str) -> dict[str, Any]:
    """把一条链编译成一条 Airflow DAG（含周期调度）。

    Returns:
        dict: {
            "pipeline_id": str,
            "compiled_dag_id": str,
            "schedule_cron": str,
            "steps": list[dict],  # 各步的 dag_id/dag_run_id 提取
            "dag_path": str,
            "spec_path": str,
        }

    Raises:
        PipelineCompileError: 未满足编译前提（步骤未确认/未执行/spec 变更）
        LookupError: pipeline 不存在
    """
    pipeline = _pipeline_service.require(db, pipeline_id)
    if not pipeline.schedule_cron:
        raise PipelineCompileError("任务链未配置 schedule_cron，请先设置周期")

    detail = _pipeline_service.detail(db, pipeline_id)
    steps = detail["steps"]
    artifacts_map = _get_artifacts_map(db, steps)

    # P3-2：拓扑排序检测环（DAG 形态下避免循环依赖）
    _validate_dag_topology(steps)

    # 校验：所有步骤已确认、已执行、spec 未变更
    _validate_ready_to_compile(steps, artifacts_map)

    # 提取各步的 DAG 信息（materialize 有批次、transform/metric 单 DAG）
    step_dags = _extract_step_dags(steps, artifacts_map)

    # 生成链 DAG（串联各步，TriggerDagRunOperator 主动触发并等其跑完）
    airflow = _settings.get_airflow_runtime(db)
    if not airflow.available:
        raise PipelineCompileError("未配置可用的 Airflow，无法编译")

    compiled_dag_id = _chain_dag_id(pipeline_id)
    dag_source = _render_chain_dag(
        dag_id=compiled_dag_id,
        schedule=pipeline.schedule_cron,
        steps=step_dags,
        pipeline_name=pipeline.name,
    )

    # 投递 DAG。与 materialize/transform/metric 一致，按 <dags_dir>/ontometa/<id>/ 子目录
    # 聚合，并走同一个投递器 —— 链 DAG 此前直接 open().write() 写本地文件系统，在
    # ontoMeta 与 Airflow 不同机时产物根本到不了 Airflow 主机。
    import os

    dag_filename = f"{compiled_dag_id}.py"
    spec_filename = f"{compiled_dag_id}.json"
    out_dir = os.path.join(airflow.dags_dir, "ontometa", compiled_dag_id)
    spec_content = {
        "pipeline_id": pipeline_id,
        "compiled_dag_id": compiled_dag_id,
        "schedule_cron": pipeline.schedule_cron,
        "steps": step_dags,
        "compiled_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        # 链 DAG 只有骨架：没有 SQL（job_files）也没有 jar（lib_files），
        # 它调度的是别的 DAG（TriggerDagRunOperator），不自己跑 Flink。
        result = airflow.build_delivery().deliver(
            dags_dir=out_dir,
            jobs_dir=os.path.join(out_dir, "jobs"),
            dag_filename=dag_filename,
            dag_source=dag_source,
            spec_filename=spec_filename,
            spec=spec_content,
            job_files={},
        )
    except Exception as exc:  # noqa: BLE001 —— 投递失败（含 OSError / DagDeliveryError）
        raise PipelineCompileError(f"DAG 投递失败：{exc}") from exc

    # 远端路径（用户拿它去 Airflow 主机上找文件）
    dag_path = result.files_written.get("dag", "")
    spec_path = result.files_written.get("spec", "")

    # 更新链的 compiled 字段
    pipeline.compiled_dag_id = compiled_dag_id
    pipeline.compiled_at = datetime.now(timezone.utc)
    db.commit()

    return {
        "pipeline_id": pipeline_id,
        "compiled_dag_id": compiled_dag_id,
        "schedule_cron": pipeline.schedule_cron,
        "steps": step_dags,
        "dag_path": dag_path,
        "spec_path": spec_path,
    }


def _chain_dag_id(pipeline_id: str) -> str:
    """链编译后的 DAG id：ontometa_chain_<pipeline_id前12位>。"""
    short = (pipeline_id or "").replace("-", "")[:12]
    return f"ontometa_chain_{short}"


def _validate_dag_topology(steps: list[dict]) -> None:
    """P3-2：拓扑排序检测环。DAG 形态下，步骤间不能循环依赖。

    算法：Kahn 拓扑排序（BFS）。入度为 0 的节点入队，逐个删除并减下游入度，
    若最终排序结果不含全部节点，则有环。
    """
    from collections import defaultdict, deque

    # 先收集所有步骤索引
    all_indices = {step["step_index"] for step in steps}

    # 构造邻接表和入度表
    graph: dict[int, list[int]] = defaultdict(list)
    in_degree: dict[int, int] = {i: 0 for i in all_indices}

    for step in steps:
        idx = step["step_index"]
        depends_on = step.get("depends_on") or []
        if not depends_on:
            # 线性默认：依赖上一步（按 steps 顺序）
            pos = next((i for i, s in enumerate(steps) if s["step_index"] == idx), None)
            if pos is not None and pos > 0:
                depends_on = [steps[pos - 1]["step_index"]]

        for up_idx in depends_on:
            if up_idx not in all_indices:
                raise PipelineCompileError(
                    f"第 {idx + 1} 步依赖的上游步序 {up_idx} 不存在"
                )
            graph[up_idx].append(idx)
            in_degree[idx] += 1

    # Kahn 拓扑排序
    queue = deque([i for i in all_indices if in_degree[i] == 0])
    sorted_order = []
    while queue:
        curr = queue.popleft()
        sorted_order.append(curr)
        for down in graph[curr]:
            in_degree[down] -= 1
            if in_degree[down] == 0:
                queue.append(down)

    if len(sorted_order) != len(all_indices):
        # 有环：找出剩下的节点（入度非 0）
        remaining = [i for i in all_indices if in_degree[i] > 0]
        raise PipelineCompileError(
            f"链步骤间存在循环依赖，涉及步序：{remaining}"
        )


def _get_artifacts_map(db: Session, steps: list[dict]) -> dict[str, GovernanceArtifact]:
    """批量查制品（避免 N+1）。"""
    ids = [s["artifact_id"] for s in steps if s["artifact_id"]]
    if not ids:
        return {}
    rows = db.query(GovernanceArtifact).filter(GovernanceArtifact.id.in_(ids)).all()
    return {r.id: r for r in rows}


def _validate_ready_to_compile(steps: list[dict], artifacts: dict[str, GovernanceArtifact]):
    """校验编译前提：所有步骤已确认、已执行、spec 未变更。"""
    for step in steps:
        artifact_id = step["artifact_id"]
        if not artifact_id:
            raise PipelineCompileError(
                f"第 {step['step_index'] + 1} 步（{step['kind']}）尚未起草，请先 advance"
            )
        artifact = artifacts.get(artifact_id)
        if not artifact:
            raise PipelineCompileError(
                f"第 {step['step_index'] + 1} 步的制品 {artifact_id} 不存在"
            )
        if artifact.status not in (
            ArtifactStatus.CONFIRMED.value,
            ArtifactStatus.SUCCEEDED.value,
        ):
            raise PipelineCompileError(
                f"第 {step['step_index'] + 1} 步（{step['kind']}）尚未确认"
                f"（当前 {artifact.status}），编译前请先确认"
            )
        if not artifact.execution_receipt_json:
            raise PipelineCompileError(
                f"第 {step['step_index'] + 1} 步（{step['kind']}）尚未执行过，"
                "编译前请先执行一次验证 spec 可行"
            )
        # spec 变更检测：updated_at > confirmed_at 说明确认后又改了。
        # 仅对 confirmed（未执行）态生效——succeeded 的 updated_at bump 来自 execute() 写
        # 回执/状态，不是 spec 变更；而 edit() 对 confirmed/succeeded 一律拒改（409），
        # 故执行后 spec 根本改不动，此检测对 succeeded 是纯误报。
        if (
            artifact.status == ArtifactStatus.CONFIRMED.value
            and artifact.confirmed_at
            and artifact.updated_at > artifact.confirmed_at
        ):
            raise PipelineCompileError(
                f"第 {step['step_index'] + 1} 步（{step['kind']}）的 spec 在确认后发生变更，"
                "请重新确认或重新编译"
            )


def _extract_step_dags(steps: list[dict], artifacts: dict[str, GovernanceArtifact]) -> list[dict]:
    """提取各步的 DAG 信息（materialize 有批次、transform/metric 单 DAG）。

    P3-2：带上 depends_on（依赖的上游步序）支持 DAG 形态。
    """
    step_dags = []
    for step in steps:
        artifact = artifacts[step["artifact_id"]]
        receipt = json.loads(artifact.execution_receipt_json or "{}")
        kind = step["kind"]

        if kind == "materialize":
            # materialize 有 batches，每个 batch 一个 DAG
            batches = receipt.get("batches") or []
            dag_ids = [b.get("dag_id") for b in batches if b.get("dag_id")]
            if not dag_ids:
                # fallback：单 DAG 回执
                dag_id = receipt.get("dag_id")
                if dag_id:
                    dag_ids = [dag_id]
        else:
            # transform / metric / sync 单 DAG
            dag_id = receipt.get("dag_id")
            dag_ids = [dag_id] if dag_id else []

        if not dag_ids:
            raise PipelineCompileError(
                f"第 {step['step_index'] + 1} 步（{kind}）的回执里没有 dag_id，"
                "可能提交未成功或制品类型不支持编译"
            )

        # P3-2：depends_on 从 step 提取（depends_on_json）
        depends_on = step.get("depends_on") or []
        step_dags.append({
            "step_index": step["step_index"],
            "kind": kind,
            "artifact_id": step["artifact_id"],
            "dag_ids": dag_ids,
            "depends_on": depends_on,
        })
    return step_dags


def _render_chain_dag(
    dag_id: str, schedule: str, steps: list[dict], pipeline_name: str
) -> str:
    """生成链 DAG 的 Python 源码（TriggerDagRunOperator 串联各步 DAG）。

    每一步的每个子 DAG 用一个 TriggerDagRunOperator 主动触发并等待完成；
    下一步的触发器依赖上一步全部触发器成功（Airflow 默认 all_success），
    故上游本周期失败时下游不会触发。链 DAG 挂 cron 周期跑，reset_dag_run=True
    保证同一 execution_date 重跑时不因 run_id 冲突而失败。

    P3-2：支持 DAG 形态。步骤若声明了 depends_on（上游步序列表），按它串依赖；
    未声明则沿用线性默认（依赖上一步）。扇出=一个上游被多个下游依赖，
    汇聚=一个下游依赖多个上游。
    """
    imports = """
from __future__ import annotations
import pendulum
from airflow import DAG
from airflow.operators.trigger_dagrun import TriggerDagRunOperator
"""

    dag_def = f'''
with DAG(
    dag_id="{dag_id}",
    description="ontoMeta 任务链：{pipeline_name}",
    schedule="{schedule}",
    start_date=pendulum.datetime(2024, 1, 1, tz="UTC"),
    catchup=False,
    max_active_runs=1,
    tags=["ontometa", "pipeline", "chain"],
) as dag:
'''

    # 先渲染所有触发器，并记住每步 step_index → 它的触发器 id 列表
    tasks: list[str] = []
    triggers_by_step: dict[int, list[str]] = {}
    ordered_indices: list[int] = []
    for step in steps:
        step_idx = step["step_index"]
        kind = step["kind"]
        dag_ids = step["dag_ids"]
        ordered_indices.append(step_idx)

        step_triggers: list[str] = []
        for j, child_dag_id in enumerate(dag_ids):
            trigger_id = f"run_step{step_idx}_{kind}_dag{j}"
            trigger_code = f'''    {trigger_id} = TriggerDagRunOperator(
        task_id="{trigger_id}",
        trigger_dag_id="{child_dag_id}",
        # 等被触发的 DAG 跑完，其失败即本任务失败——下游据此不触发。
        wait_for_completion=True,
        poke_interval=60,
        # 触发失败态即上抛，不把失败吞成成功。
        failed_states=["failed"],
        allowed_states=["success"],
        # 同一 execution_date 重跑时清掉旧 run，避免 run_id 冲突。
        reset_dag_run=True,
    )
'''
            tasks.append(trigger_code)
            step_triggers.append(trigger_id)
        triggers_by_step[step_idx] = step_triggers

    # 串依赖：按 depends_on（声明了则用它，否则沿用线性上一步）。
    for pos, step in enumerate(steps):
        step_idx = step["step_index"]
        depends_on = step.get("depends_on") or []
        if not depends_on and pos > 0:
            # 线性默认：依赖上一步（按 steps 顺序）
            depends_on = [steps[pos - 1]["step_index"]]
        for up_idx in depends_on:
            for prev_id in triggers_by_step.get(up_idx, []):
                for curr_id in triggers_by_step[step_idx]:
                    tasks.append(f"    {prev_id} >> {curr_id}\n")

    return imports + dag_def + "\n".join(tasks)
