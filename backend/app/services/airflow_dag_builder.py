"""搬运作业 + 建表 DDL → Airflow DAG（M10）。

**投递方式为「生成 DAG 文件」（方案 A）**：产物即治理制品，可 diff、可 review、可回滚，
且 Airflow 解析期不依赖 ontoMeta 在线。见 `MATERIALIZE_ORCHESTRATION.md` §5。

**DAG 文件 + 边车 JSON**：DAG 只有骨架（任务怎么连、用什么 Operator），作业清单与 DDL
放同目录的 JSON。真实 ERP 本体有 734 张表，把配置内联进 .py 会得到一个没人能 review 的
巨型文件；拆开后 DAG 稳定、JSON 可 diff，两者都是制品。

**任务编排**::

    create_tables ──> [dim 层的搬运任务…] ──> [dwd 层的搬运任务…]

- ``create_tables`` 跑 M3 生成的 DDL。**建表绝不交给搬运工具**——本体反补的注释、分区、
  主键声明只在那条路径上，让 sink 自动建表就全丢了。
- 同层内的搬运任务**彼此独立、可并行**：每个任务各自从源系统读、写各自的目标表。
  层间串行只为并发闸门与失败早停，不是数据依赖（见 ``JobPlan`` 的说明）。
- 任务上声明 ``inlets``/``outlets``，DataHub 的 Airflow 插件据此自动上报血缘（M11）。

凭据不进产物：DAG 里只有 ``conn_id`` 与 ``${别名_XXX}`` 占位符。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from app.warehouse.jobs import JobPlan, JobSpec, get_job_adapter

# 与 docker/orchestration/docker-compose.yml 的挂载点一致。
DEFAULT_JOBS_MOUNT = "/opt/seatunnel/jobs"
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
    # {文件名: SeaTunnel 作业配置}，落到 jobs 目录供任务容器读取
    job_files: dict[str, dict] = field(default_factory=dict)

    def write(self, dags_dir: str, jobs_dir: str) -> dict[str, str]:
        """落盘，返回 {用途: 绝对路径}。目录不存在即创建。"""
        import os

        os.makedirs(dags_dir, exist_ok=True)
        os.makedirs(jobs_dir, exist_ok=True)
        written: dict[str, str] = {}

        dag_path = os.path.join(dags_dir, self.dag_filename)
        with open(dag_path, "w", encoding="utf-8") as fh:
            fh.write(self.dag_source)
        written["dag"] = dag_path

        spec_path = os.path.join(dags_dir, self.spec_filename)
        with open(spec_path, "w", encoding="utf-8") as fh:
            json.dump(self.spec, fh, ensure_ascii=False, indent=2, sort_keys=True)
        written["spec"] = spec_path

        for name, conf in sorted(self.job_files.items()):
            job_path = os.path.join(jobs_dir, name)
            with open(job_path, "w", encoding="utf-8") as fh:
                json.dump(conf, fh, ensure_ascii=False, indent=2, sort_keys=True)
        written["jobs_dir"] = jobs_dir
        return written


def dag_id_for(ontology_id: str) -> str:
    """稳定的 DAG id：同一本体反复物化更新同一个 DAG，不堆积垃圾 DAG。"""
    short = (ontology_id or "").replace("-", "")[:12]
    return f"ontometa_materialize_{short}"


def _task_group_order(jobs: tuple[JobSpec, ...]) -> list[str]:
    layers = {j.layer for j in jobs}
    known = [layer for layer in _LAYER_ORDER if layer in layers]
    return known + sorted(layers - set(known))


class AirflowDagBuilder:
    def build(
        self,
        *,
        ontology_id: str,
        plan: JobPlan,
        ddl_statements: dict[str, str],
        schedule: str | None = None,
        tool: str | None = None,
        engine: str = "hive",
        warehouse_conn_id: str = DEFAULT_WAREHOUSE_CONN_ID,
        jobs_mount: str = DEFAULT_JOBS_MOUNT,
        target_urn_builder=None,
    ) -> DagBundle:
        """产出 DAG 包。

        ``tool`` 决定搬运工具（seatunnel/datax/flink）——镜像与运行命令都由该工具的
        Adapter 提供，DAG 骨架对工具无感（只读每个任务自带的 ``image``/``command``）。
        ``schedule`` 为契约的 refresh_cron（空 = 只手动触发）；
        ``target_urn_builder`` 供 M11 注入目标表 URN 构造（需部署环境的 fabric，M10 不臆造）。
        """
        adapter = get_job_adapter(tool)
        dag_id = dag_id_for(ontology_id)

        job_files: dict[str, dict] = {}
        tasks: list[dict] = []
        for job in plan.jobs:
            filename = f"{dag_id}__{job.name}.json"
            job_files[filename] = adapter.render(job)
            config_file = f"{jobs_mount}/{filename}"
            tasks.append(
                {
                    "task_id": job.name,
                    "layer": job.layer,
                    "config_file": config_file,
                    # 镜像与命令由工具 Adapter 提供，DAG 骨架据此起 DockerOperator，
                    # 换工具只改这两个字段，不动骨架——这是「工具可插拔」的落地形式。
                    "image": adapter.docker_image,
                    "command": adapter.airflow_command(config_file),
                    "target": job.target.qualified,
                    "mode": job.mode,
                    # 血缘：上游用本体的 source_ref（本就是 DataHub URN）。
                    # 下游 URN 需部署环境的 fabric，M10 不构造，留给 M11 注入。
                    "inlets": [job.source_urn] if job.source_urn else [],
                    "outlets": (
                        [target_urn_builder(job)] if target_urn_builder else []
                    ),
                }
            )

        spec = {
            "dag_id": dag_id,
            "ontology_id": ontology_id,
            "engine": engine,
            "tool": adapter.name,
            "schedule": schedule or None,
            "warehouse_conn_id": warehouse_conn_id,
            # 建表语句按目标表名排序，保证幂等
            "ddl": [ddl_statements[k] for k in sorted(ddl_statements)],
            "ddl_targets": sorted(ddl_statements),
            "tasks": tasks,
            "layer_order": _task_group_order(plan.jobs),
            "unsupported": plan.unsupported,
            "schema_notes": plan.schema_notes,
        }
        return DagBundle(
            dag_id=dag_id,
            dag_filename=f"{dag_id}.py",
            dag_source=_render_dag_source(dag_id, f"{dag_id}.json"),
            spec_filename=f"{dag_id}.json",
            spec=spec,
            job_files=job_files,
        )


# DAG 骨架。内容不随本体变化——变的全在边车 JSON 里，故这个文件天然稳定、易 review。
_DAG_TEMPLATE = '''"""ontoMeta 物化 DAG（自动生成，勿手改）。

由 ``app/services/airflow_dag_builder.py`` 生成，同目录的 {spec_filename} 是它的输入。
重新物化会覆盖这两个文件；手改会在下次提交时丢失。

任务编排：create_tables → 各层搬运任务（层间串行、层内并行）。
建表跑的是 ontoMeta 按本体生成的 DDL：表注释/分区/主键声明由本体反补，
**不允许**让搬运工具的 auto-create schema 绕过去。
"""

from __future__ import annotations

import json
import pathlib

import pendulum
from airflow import DAG
from airflow.providers.common.sql.operators.sql import SQLExecuteQueryOperator
from airflow.providers.docker.operators.docker import DockerOperator

_SPEC = json.loads(
    (pathlib.Path(__file__).with_name("{spec_filename}")).read_text(encoding="utf-8")
)

with DAG(
    dag_id="{dag_id}",
    description="ontoMeta 物化：建表 + 按本体映射搬运",
    schedule=_SPEC.get("schedule"),
    start_date=pendulum.datetime(2024, 1, 1, tz="UTC"),
    catchup=False,
    max_active_runs=1,
    tags=["ontometa", "materialize"],
) as dag:
    create_tables = SQLExecuteQueryOperator(
        task_id="create_tables",
        conn_id=_SPEC["warehouse_conn_id"],
        sql=_SPEC["ddl"],
    )

    by_layer: dict[str, list] = {{}}
    for task in _SPEC["tasks"]:
        op = DockerOperator(
            task_id=task["task_id"],
            image=task["image"],
            # 镜像与命令由搬运工具（seatunnel/datax/flink）的 Adapter 产出；
            # command 是 DockerOperator 的模板字段，里面的 {{{{ data_interval_start }}}}
            # 由 Airflow 在运行时渲染为本次数据区间起点（水位），补数自动回区间。
            command=task["command"],
            mounts_tmp_dir=False,
            auto_remove="success",
            network_mode="bridge",
            inlets=task["inlets"],
            outlets=task["outlets"],
        )
        by_layer.setdefault(task["layer"], []).append(op)

    previous = create_tables
    for layer in _SPEC["layer_order"]:
        group = by_layer.get(layer) or []
        if not group:
            continue
        previous >> group
        previous = group
'''


def _render_dag_source(dag_id: str, spec_filename: str) -> str:
    return _DAG_TEMPLATE.format(dag_id=dag_id, spec_filename=spec_filename)


dag_builder = AirflowDagBuilder()
