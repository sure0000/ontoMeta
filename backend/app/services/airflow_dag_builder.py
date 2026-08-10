"""Flink SQL DAG 构建器（统一执行架构）——搬运作业 → Airflow DAG。

**投递方式为「生成 DAG 文件」（方案 A）**：产物即治理制品，可 diff、可 review、可回滚，
且 Airflow 解析期不依赖 ontoMeta 在线。

**DAG 文件 + 边车 JSON**：DAG 只有骨架（任务怎么连、用什么 Operator），Flink SQL /
DDL 放同目录的 JSON。真实本体有 734 张表，把 SQL 内联进 .py 会得到一个巨型文件；
拆开后 DAG 稳定、JSON 可 diff，两者都是制品。

**统一执行架构**：搬运 = Flink SQL on YARN（与 transform/metric 同一执行路径），不再有
runner/docker 多通道、不再有 SeaTunnel/DataX 工具选择。本模块只保留 Flink DAG 构建逻辑，
docker/runner 旧通道代码已删除（见统一执行 F/G 阶段）。
"""

from __future__ import annotations

import hashlib
import json as _json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from app.warehouse.jobs import JobSpec
from app.warehouse.logical_schema import LogicalTable
from app.warehouse.registry import get_adapter

DEFAULT_WAREHOUSE_CONN_ID = "warehouse_default"
# 层的执行顺序；未列出的层排在最后（顺序稳定，保证幂等）。
_LAYER_ORDER = ("dim", "dwd", "dws", "ads")


@dataclass
class DagBundle:
    """一次物化的全部产物。三个文件都要落盘、都可 diff。"""

    dag_id: str
    dag_filename: str
    dag_source: str
    spec_filename: str
    spec: dict


@dataclass
class _Staging:
    """全量装载的 staging 计划（staging DDL + swap 语句 + 改写后的搬运作业）。"""

    ddl: list[str] = field(default_factory=list)  # staging 表 DDL
    swaps: dict[str, list[str]] = field(default_factory=dict)  # 任务 ID → swap SQL
    exec_jobs: dict[str, JobSpec] = field(default_factory=dict)  # 改写后的作业（写 staging）


def _plan_staging(plan, *, engine: str, token: str, enabled: bool) -> _Staging:
    """全量装载改走 staging + 原子切换（M15）。

    **只作用于 full**：增量是往正式表追加，搬进 staging 再整表切换会把存量数据换没。

    staging 名里的 token 用**批次后缀**而非 run_id：同一个 DAG 的 ``max_active_runs=1``
    保证两次运行不重叠，而一张表只属于一个批次，故这个名字已经不会撞；用 run_id 反而会
    让每次失败的运行留下一张不会被回收的 staging 表，且 DAG 产物里要塞 Jinja 表达式。
    """
    from dataclasses import replace

    staging = _Staging()
    if not enabled:
        return staging
    adapter = get_adapter(engine)
    for job in plan.jobs:
        if job.mode != "full":
            continue
        table = LogicalTable(name=job.target.table, database=job.target.database)
        staging.ddl.append(
            _as_single_statement(adapter.render_create_staging(table, token))
        )
        staging.swaps[job.name] = [
            _as_single_statement(s) for s in adapter.render_swap(table, token)
        ]
        staging.exec_jobs[job.name] = replace(
            job,
            target=replace(
                job.target, table=adapter.staging_table_name(table, token)
            ),
        )
    return staging


def _as_single_statement(sql: str) -> str:
    """DDL 一定是单语句；多余的分号会让某些引擎（Hive）重复执行。去掉末尾分号。"""
    return sql.rstrip().rstrip(";")


def _constraint_list(constraints: dict[str, list[str]] | None) -> list[str]:
    """把表 → 约束列表的映射展平成顺序确定的约束 SQL 列表（加外键 / 主键声明）。

    key 是表的 qualified，value 是那张表的多条约束语句（ALTER TABLE ADD ...）。
    每张表可能有多条，所有表的约束平铺成一个列表、按表名排序，顺序稳定（幂等）。
    """
    if not constraints:
        return []
    flattened = []
    for table in sorted(constraints):
        flattened.extend(constraints[table])
    return flattened


@dataclass(frozen=True)
class FlinkSubmitConfig:
    """Flink 提交的基础配置（SqlRunner JAR、YARN 队列、checkpoint 目录）。"""

    sql_runner_jar: str  # SqlRunner JAR 路径（file://… 或 hdfs://…）
    yarn_queue: str = "default"
    checkpoint_dir: str = ""  # checkpoint 目录（file://… 或 hdfs://…），增量/CDC 需要


@dataclass(frozen=True)
class FlinkSqlTask:
    """一个 Flink SQL 搬运任务的完整声明（SQL + 端点凭据占位符 + 提交参数）。"""

    task_id: str
    sql: str  # 完整 Flink SQL（INSERT INTO … SELECT …），含 ${} 占位符
    endpoint_env: dict[str, str] = field(default_factory=dict)  # 端点凭据环境变量
    detached: bool = False  # 流式作业（incremental/cdc）需 -d
    checkpoint_dir: str = ""  # 流式作业的 checkpoint 目录


def flink_dag_id_for(base: str, suffix: str | None = None) -> str:
    """Flink DAG ID 生成规则：base[_suffix]_flink。"""
    parts = [base]
    if suffix:
        parts.append(suffix)
    parts.append("flink")
    return "_".join(parts)


def _flink_run_command(
    task: FlinkSqlTask, config: FlinkSubmitConfig, sql_file: str
) -> list[str]:
    """构造 `flink run` 命令（传给 BashOperator）。"""
    cmd = ["flink", "run"]
    if config.yarn_queue:
        cmd.extend(["-yqu", config.yarn_queue])
    if task.detached:
        cmd.append("-d")  # 流式作业 detached 运行
    cmd.extend(["-c", "com.ontometa.flink.SqlRunner", config.sql_runner_jar, sql_file])
    return cmd


def build_flink_sql_dag(
    *,
    ontology_id: str,
    engine: str,
    tasks: list[FlinkSqlTask],
    ddl_statements: dict[str, str],
    constraints: dict[str, list[str]] | None = None,
    swaps: dict[str, list[str]] | None = None,
    config: FlinkSubmitConfig,
    schedule: str | None = None,
    dag_id_suffix: str | None = None,
    warehouse_conn_id: str = DEFAULT_WAREHOUSE_CONN_ID,
    max_active_tasks: int = 16,
    target_urn_builder: Any = None,
) -> DagBundle:
    """构建 Flink SQL DAG 包（统一执行架构的唯一搬运路径）。

    - ``tasks``：Flink SQL 搬运任务列表（每个任务 = 一条 INSERT INTO … SELECT …）。
    - ``ddl_statements``：建表 DDL（qualified 表名 → DDL SQL）。
    - ``constraints``：外键/主键约束（qualified 表名 → 约束 SQL 列表），在所有表建完后加。
    - ``swaps``：staging swap 语句（任务 ID → swap SQL 列表），全量表用 staging+原子切换。
    - ``config``：Flink 提交配置（JAR / YARN 队列 / checkpoint 目录）。
    - ``schedule``：cron 表达式；None = 手动触发。
    - ``dag_id_suffix``：DAG ID 后缀（用于同一本体的多批次）。
    - ``warehouse_conn_id``：建表用的 Airflow Connection ID。
    - ``max_active_tasks``：DAG 最大并行任务数。
    - ``target_urn_builder``：目标表 URN 构造器（用于血缘 outlets）。

    **DAG 结构**::

        create_tables ──> [搬运任务…] ──> swap_<task> ──> _tails (仅全量表有 swap)
                      ├──> [增量/CDC 任务…] (无 swap，直接 INSERT)
                      └──> add_constraints (外键/主键，所有表建完后加)

    - ``create_tables``：跑 M3 生成的 DDL。**建表绝不交给搬运工具**。
    - 搬运任务：BashOperator 跑 `flink run`，SQL 文件从边车 JSON 读。
    - ``swap_<task>``：staging 切换任务（RENAME staging → 正式表）。
    - ``_tails``：虚拟任务，等所有 swap 完成（下游依赖挂这里）。
    - ``add_constraints``：加外键/主键（依赖 create_tables，所有表建完后跑）。

    **为什么没有任务分层**：Flink SQL 已处理依赖（源表 JOIN），不需要 dim → dwd 的硬排序。
    只保证「先建表、再搬运、swap 在搬完后、约束最后加」。

    **Staging swap**：全量表写 staging、原子切换；增量/CDC 直接写正式表（APPEND）。
    swap 的 SQL 由 materialization_runner._plan_staging 产出，本函数只负责编排。
    """
    dag_id = flink_dag_id_for(ontology_id, dag_id_suffix)
    # 搬运任务列表
    task_specs: list[dict] = []
    for task in tasks:
        # 每个任务 = task_id + SQL 文件名 + 端点凭据 + flink run 命令
        sql_filename = f"{task.task_id}.sql"
        task_specs.append(
            {
                "task_id": task.task_id,
                "sql_file": sql_filename,
                "sql": task.sql,
                "endpoint_env": task.endpoint_env,
                "command": _flink_run_command(task, config, f"{{{{ ti.xcom_pull(task_ids='read_spec')['sql_dir'] }}}}/{sql_filename}"),
                "detached": task.detached,
                # 血缘：上游用 source_urn（如有），下游用 target URN（如有 builder）。
                # 这里简化：Flink SQL 任务的血缘由 SQL 解析决定，暂不注入 inlets/outlets。
            }
        )

    # Staging swap 任务（全量表才有）
    swap_specs: dict[str, list[str]] = {}
    if swaps:
        for task_id, swap_sqls in swaps.items():
            swap_specs[task_id] = [_as_single_statement(s) for s in swap_sqls]

    # 边车 JSON spec
    spec = {
        "dag_id": dag_id,
        "ontology_id": ontology_id,
        "engine": engine,
        "schedule": schedule or None,
        "warehouse_conn_id": warehouse_conn_id,
        "max_active_tasks": max_active_tasks,
        "warehouse_ddl": [_as_single_statement(ddl_statements[k]) for k in sorted(ddl_statements)],
        "ddl_targets": sorted(ddl_statements),
        "constraints": _constraint_list(constraints),
        "tasks": task_specs,
        "swaps": swap_specs,
        "flink_config": {
            "sql_runner_jar": config.sql_runner_jar,
            "yarn_queue": config.yarn_queue,
            "checkpoint_dir": config.checkpoint_dir,
        },
    }

    # DAG 源码（Python 模板）
    dag_source = _render_flink_dag_source(dag_id, f"{dag_id}.json")
    dag_filename = f"{dag_id}.py"
    spec_filename = f"{dag_id}.json"

    return DagBundle(
        dag_id=dag_id,
        dag_filename=dag_filename,
        dag_source=dag_source,
        spec_filename=spec_filename,
        spec=spec,
    )


def _render_flink_dag_source(dag_id: str, spec_filename: str) -> str:
    """渲染 Flink DAG 的 Python 源码（Airflow DAG 文件）。

    DAG 文件只有骨架（任务怎么连、用什么 Operator），SQL / DDL 从边车 JSON 读。
    这样 DAG 文件稳定（可 review）、JSON 可 diff（734 张表的 SQL 不内联进 .py）。
    """
    # DAG 模板（BashOperator 跑 flink run，SQL 文件从 JSON 读）
    return f'''# Flink SQL DAG: {dag_id}
# 统一执行架构：搬运走 Flink SQL on YARN（与 transform/metric 同一执行路径）
# 产物：本 DAG 文件 + 边车 JSON（{spec_filename}）
# 生成时间：{{{{ macros.datetime.now().isoformat() }}}}

from airflow import DAG
from airflow.operators.python import PythonOperator
from airflow.operators.bash import BashOperator
from airflow.providers.common.sql.operators.sql import SQLExecuteQueryOperator
from airflow.utils.task_group import TaskGroup
from datetime import datetime, timedelta
import json
from pathlib import Path

# 读边车 JSON spec
_SPEC_PATH = Path(__file__).parent / "{spec_filename}"
_SPEC = json.loads(_SPEC_PATH.read_text(encoding="utf-8"))

default_args = {{{{
    "owner": "ontoMeta",
    "depends_on_past": False,
    "email_on_failure": False,
    "email_on_retry": False,
    "retries": 1,
    "retry_delay": timedelta(minutes=5),
}}}}

with DAG(
    dag_id="{dag_id}",
    default_args=default_args,
    description=f"Flink SQL 搬运：{{{{ _SPEC['ontology_id'] }}}}",
    schedule=_SPEC.get("schedule"),
    start_date=datetime(2024, 1, 1),
    catchup=False,
    max_active_tasks=_SPEC.get("max_active_tasks", 16),
    tags=["ontometa", "flink", "sync"],
) as dag:

    # 1. 读 spec，暴露 sql_dir 给下游（SQL 文件都在这个目录）
    def _read_spec(**context):
        return {{{{
            "spec": _SPEC,
            "sql_dir": str(_SPEC_PATH.parent),
        }}}}

    read_spec = PythonOperator(
        task_id="read_spec",
        python_callable=_read_spec,
    )

    # 2. 建表（M3 生成的 DDL，**绝不交给搬运工具自动建**）
    create_tables = SQLExecuteQueryOperator(
        task_id="create_tables",
        conn_id=_SPEC["warehouse_conn_id"],
        sql=_SPEC["warehouse_ddl"],
        split_statements=True,
    )

    # 3. 搬运任务（BashOperator 跑 flink run）
    move_tasks = {{{{}}}}
    for task_spec in _SPEC["tasks"]:
        # 端点凭据从 Airflow Connection 读，注入环境变量（$别名_URL / $别名_USER 等）
        env_vars = task_spec.get("endpoint_env", {{}})
        # 构造 flink run 命令
        cmd = " ".join(task_spec["command"])
        move_task = BashOperator(
            task_id=task_spec["task_id"],
            bash_command=cmd,
            env=env_vars,
        )
        move_tasks[task_spec["task_id"]] = move_task
        # 搬运依赖建表
        create_tables >> move_task

    # 4. Staging swap（全量表才有，增量/CDC 无）
    swaps = _SPEC.get("swaps", {{}})
    swap_tasks = {{{{}}}}
    for task_id, swap_sqls in swaps.items():
        swap_task = SQLExecuteQueryOperator(
            task_id=f"swap_{{{{task_id}}}}",
            conn_id=_SPEC["warehouse_conn_id"],
            sql=swap_sqls,
            split_statements=True,
        )
        swap_tasks[task_id] = swap_task
        # swap 依赖对应的搬运任务
        move_tasks[task_id] >> swap_task

    # 5. _tails 虚拟任务（等所有 swap 完成，下游依赖挂这里）
    if swap_tasks:
        from airflow.operators.empty import EmptyOperator
        tails = EmptyOperator(task_id="_tails")
        for swap_task in swap_tasks.values():
            swap_task >> tails

    # 6. 加外键/主键（所有表建完后跑）
    constraints = _SPEC.get("constraints", [])
    if constraints:
        add_constraints = SQLExecuteQueryOperator(
            task_id="add_constraints",
            conn_id=_SPEC["warehouse_conn_id"],
            sql=constraints,
            split_statements=True,
        )
        create_tables >> add_constraints
'''
