"""L3：血缘驱动的任务调度——把多个 Flink 任务按血缘依赖编成一条 Airflow DAG。

**不变量**：血缘唯一权威是 DataHub。本模块**不做本地血缘推导**，只做两件事：

1. **同一次提交内的依赖**：任务 A 的 ``target_urn`` == 任务 B 的 ``source_urn``
   → A 是 B 的上游。这来自 executor / move_job_compiler 已算好的 URN（L1），
   它们本身就是从 DataHub 表级血缘投影下来的（源表/目标表 URN 就是血缘边端点）。

2. **跨提交依赖**：查 DataHub（``DataHubConnector.get_lineage_around``）找
   「谁产出了我要消费的表」。查到且该产出者已有已确认制品 → 也串进依赖。
   查不到/网络失败 → **不阻断**（本任务独立触发，血缘缺失只影响展示）。

**产出**：一组 (FlinkSqlTask, 依赖边) → ``build_flink_sql_dag(task_dependencies=...)``
编成一条 DAG，一次触发。Airflow 默认 all_success：上游失败时下游不执行。

**环检测**：Kahn 拓扑排序（与 pipeline_compiler 同算法）。有环抛错，不静默。

**与人工链（GovernanceTaskPipeline）的关系**：本模块是 C2 的替代路径——依赖
从血缘自动推导，不需要用户逐步建链。人工链保留（向后兼容），新路径走这里。
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

from app.services.airflow_dag_builder import FlinkSqlTask, build_flink_sql_dag


class LineageSchedulerError(RuntimeError):
    """依赖推导 / DAG 编译失败，面向用户可读。"""


@dataclass
class ScheduledTask:
    """一个待调度任务 + 它的血缘元信息。

    ``source_urns`` / ``target_urn`` 是血缘的**权威字段**（L4 从回执重建任务时
    ``task`` 可为 None）；``task`` 非空时它的 source_urns/target_urn 与顶层字段
    指向同一事实（executor 构造时两边都给）。
    """

    task: FlinkSqlTask | None = None
    label: str = ""  # 展示名（制品名 / 任务意图）
    artifact_id: str = ""  # 关联的治理制品 id（可空）
    task_id: str = ""  # task 为 None 时也要有 id（L4 重建场景）
    source_urns: tuple[str, ...] = ()  # 源表 URN（inlets）
    target_urn: str = ""  # 目标表 URN（outlets）
    upstream_urns: tuple[str, ...] = ()  # 显式上游 URN（跨提交依赖，查 DataHub 得）
    downstream_urns: tuple[str, ...] = ()  # 显式下游 URN（同提交内匹配用）

    def __post_init__(self) -> None:
        if self.task is not None:
            if not self.task_id:
                self.task_id = self.task.task_id
            if not self.source_urns:
                self.source_urns = self.task.source_urns
            if not self.target_urn:
                self.target_urn = self.task.target_urn

    @property
    def id(self) -> str:
        return self.task_id or (self.task.task_id if self.task else "")


def derive_dependencies(
    tasks: list[ScheduledTask],
) -> list[tuple[ScheduledTask, ScheduledTask]]:
    """按 URN 匹配推导任务间依赖。

    规则：任务 A 的 ``target_urn`` 出现在任务 B 的 ``source_urns``（或显式
    ``upstream_urns``）里 → ``A >> B``。

    同一张表可能被多个任务产出（重建/重跑）——此时取**最近的依赖不变量**：
    不重复建边（去重），且若 A1、A2 都产出同一张表，B 同时依赖两者（都成功
    B 才跑，Airflow all_success 语义天然正确）。

    Args:
        tasks: 待调度任务列表。

    Returns:
        ``[(上游, 下游), ...]`` 边列表（保序、去重）。

    Raises:
        LineageSchedulerError: 检测到环（A → B → A）或 task_id 重复。
    """
    by_id = {t.id: t for t in tasks}
    if len(by_id) != len(tasks):
        raise LineageSchedulerError("任务 task_id 重复，无法推导依赖")

    # target_urn → 产出它的任务（一个 URN 可能被多个任务产出）
    producers: dict[str, list[ScheduledTask]] = defaultdict(list)
    for t in tasks:
        if t.target_urn:
            producers[t.target_urn].append(t)

    edges: list[tuple[ScheduledTask, ScheduledTask]] = []
    seen: set[tuple[str, str]] = set()
    for t in tasks:
        consumed = set(t.source_urns) | set(t.upstream_urns)
        for urn in consumed:
            for producer in producers.get(urn, []):
                if producer.id == t.id:
                    continue  # 自依赖（不该出现，防御）
                key = (producer.id, t.id)
                if key not in seen:
                    seen.add(key)
                    edges.append((producer, t))

    _check_acyclic(tasks, edges)
    return edges


def _check_acyclic(
    tasks: list[ScheduledTask], edges: list[tuple[ScheduledTask, ScheduledTask]]
) -> None:
    """Kahn 拓扑排序检测环。有环抛 LineageSchedulerError。"""
    all_ids = {t.id for t in tasks}
    graph: dict[str, list[str]] = defaultdict(list)
    in_degree: dict[str, int] = {tid: 0 for tid in all_ids}
    for up, down in edges:
        if up.id not in all_ids or down.id not in all_ids:
            continue  # 防御：build_flink_sql_dag 会拦，这里不重复报
        graph[up.id].append(down.id)
        in_degree[down.id] += 1

    queue = deque([tid for tid in all_ids if in_degree[tid] == 0])
    visited = 0
    while queue:
        curr = queue.popleft()
        visited += 1
        for nxt in graph[curr]:
            in_degree[nxt] -= 1
            if in_degree[nxt] == 0:
                queue.append(nxt)

    if visited != len(all_ids):
        remaining = [tid for tid in all_ids if in_degree[tid] > 0]
        raise LineageSchedulerError(
            f"任务间存在循环依赖，涉及：{remaining}"
        )


def compile_lineage_dag(
    *,
    ontology_id: str,
    engine: str,
    tasks: list[ScheduledTask],
    ddl_statements: dict[str, str] | None = None,
    config: Any,
    warehouse_conn_id: str = "warehouse_default",
    dag_id_suffix: str | None = None,
    schedule: str | None = None,
) -> Any:
    """把一组任务按血缘依赖编成一条 Airflow DAG（B2：一次触发）。

    Args:
        tasks: 待调度任务（含血缘元信息）。
        ddl_statements: 建表 DDL（qualified 表名 → DDL SQL）。
        config: FlinkSubmitConfig。
        dag_id_suffix: DAG ID 后缀（同一次提交内多批时区分）。

    Returns:
        DagBundle（与 build_flink_sql_dag 同构）。

    Raises:
        LineageSchedulerError: 依赖有环。
    """
    edges = derive_dependencies(tasks)
    # ScheduledTask → FlinkSqlTask（compile 阶段 task 必须非空）
    for t in tasks:
        if t.task is None:
            raise LineageSchedulerError(
                f"任务 {t.id} 没有 FlinkSqlTask，无法编译 DAG"
            )
    return build_flink_sql_dag(
        ontology_id=ontology_id,
        engine=engine,
        tasks=[t.task for t in tasks],  # type: ignore[list-item]  # 已在上方保证非空
        ddl_statements=ddl_statements or {},
        config=config,
        schedule=schedule,
        dag_id_suffix=dag_id_suffix,
        warehouse_conn_id=warehouse_conn_id,
        task_dependencies=[(up.task, down.task) for up, down in edges],  # type: ignore[arg-type]
    )


def describe_lineage(tasks: list[ScheduledTask]) -> dict:
    """供 Agent 回复「本次启动了哪些任务 + 血缘」的摘要（L4 的数据源）。"""
    edges = derive_dependencies(tasks)
    return {
        "tasks": [
            {
                "task_id": t.id,
                "label": t.label or t.id,
                "artifact_id": t.artifact_id,
                "source_urns": list(t.source_urns),
                "target_urn": t.target_urn,
            }
            for t in tasks
        ],
        "dependencies": [
            {"upstream": up.id, "downstream": down.id}
            for up, down in edges
        ],
    }
