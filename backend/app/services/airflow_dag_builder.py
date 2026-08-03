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

from app.warehouse.jobs import JobPlan, JobSpec, get_job_adapter, resolve_docker_image

# 作业配置在搬运容器里的挂载点。默认随工具走（各 Adapter 的 jobs_mount_dir）；
# 这个常量只是工具没声明时的兜底，与 docker/orchestration/docker-compose.yml 一致。
DEFAULT_JOBS_MOUNT = "/opt/seatunnel/jobs"
DEFAULT_WAREHOUSE_CONN_ID = "warehouse_default"
# 搬运容器的网络。默认 bridge 与 DockerOperator 原行为一致。
DEFAULT_DOCKER_NETWORK = "bridge"
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


def _driver_jars(host_dir: str | None, lib_dir: str) -> list[dict[str, str]]:
    """驱动目录里的 jar → 逐个文件的挂载对。

    **必须逐文件挂，不能整目录挂**：把宿主机目录挂到 ``/opt/seatunnel/lib`` 会把工具
    自己的 jar 全遮住，容器直接起不来。把单个文件挂进已有目录则只是新增一项。
    排序保证同一目录产出的 spec 逐字节一致（幂等）。
    """
    import os

    if not host_dir or not lib_dir or not os.path.isdir(host_dir):
        return []
    return [
        {"source": os.path.join(host_dir, name), "target": f"{lib_dir}/{name}"}
        for name in sorted(os.listdir(host_dir))
        if name.endswith(".jar")
    ]


def _as_single_statement(sql: str) -> str:
    """去掉语句末尾的分号。

    生成器产出的是**脚本形态**的 SQL（带 ``;``，便于人读与下载），而 DAG 里这些语句
    是逐条交给 DB-API ``execute()`` 的，一次只能给一条语句。Hive 对此严格，末尾多一个
    分号直接 ``ParseException: extraneous input ';' expecting EOF``（真实实例上踩过）。
    只在这个边界上剥离，不动生成器的输出。
    """
    return (sql or "").rstrip().rstrip(";").rstrip()


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
        jobs_mount: str | None = None,
        jobs_host_dir: str | None = None,
        drivers_host_dir: str | None = None,
        docker_network: str = DEFAULT_DOCKER_NETWORK,
        image_overrides: dict[str, str] | None = None,
        target_urn_builder=None,
    ) -> DagBundle:
        """产出 DAG 包。

        ``tool`` 决定搬运工具（seatunnel/datax/flink）——镜像与运行命令都由该工具的
        Adapter 提供，DAG 骨架对工具无感（只读每个任务自带的 ``image``/``command``）。
        ``image_overrides`` 是部署方对镜像的覆盖（``{工具名: 镜像}``）：无官方镜像的
        工具（DataX）没配到这里就在**这一步**抛 ``SyncImageUnavailableError``，
        绝不产出一个注定 pull 失败的 DAG。
        ``schedule`` 为契约的 refresh_cron（空 = 只手动触发）；
        ``target_urn_builder`` 供 M11 注入目标表 URN 构造（需部署环境的 fabric，M10 不臆造）。
        """
        adapter = get_job_adapter(tool)
        # 先解析镜像：拿不到就在这里失败，落盘与触发都还没发生。
        image = resolve_docker_image(adapter, image_overrides)
        jobs_mount = jobs_mount or adapter.jobs_mount_dir or DEFAULT_JOBS_MOUNT
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
                    # 镜像与命令由工具 Adapter 提供（镜像可被部署方覆盖），DAG 骨架据此
                    # 起 DockerOperator，换工具只改这两个字段，不动骨架——这是
                    # 「工具可插拔」的落地形式。
                    "image": image,
                    "command": adapter.airflow_command(config_file, adapter.credential_env(job)),
                    # 作业配置里的 ${别名_字段} 占位符靠这张表在运行期补齐。值是
                    # Jinja 表达式（{{ conn.… }}），任务跑起来才渲染——产物里不含凭据。
                    "env": adapter.credential_env(job),
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
            # 冗余记一份工具级镜像：排查「任务为什么起不来」时第一个要看的就是它，
            # 不必逐个任务翻。各任务的 image 与之相同。
            "tool_image": image,
            "schedule": schedule or None,
            "warehouse_conn_id": warehouse_conn_id,
            "docker_network": docker_network,
            # 作业配置要挂进搬运容器。任务容器是 worker 通过宿主机 docker.sock 起的
            # **兄弟容器**，所以 source 必须是宿主机路径（worker 里的挂载点对它无效）。
            "jobs_mount": jobs_mount,
            "jobs_host_dir": jobs_host_dir or "",
            # JDBC 驱动：镜像不带（授权原因），由部署方提供 jar，逐个挂进工具的 lib 目录。
            "driver_jars": _driver_jars(drivers_host_dir, adapter.driver_lib_dir),
            # 建表语句按目标表名排序，保证幂等
            "ddl": [_as_single_statement(ddl_statements[k]) for k in sorted(ddl_statements)],
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
from docker.types import Mount

_SPEC = json.loads(
    (pathlib.Path(__file__).with_name("{spec_filename}")).read_text(encoding="utf-8")
)


# 作业配置的挂载。source 是**宿主机**路径：任务容器由 worker 经 docker.sock 起，
# 对守护进程而言是兄弟容器，worker 自己的挂载点在它那儿不成立。
def _bind(host_key: str, target_key: str) -> list:
    host, target = _SPEC.get(host_key), _SPEC.get(target_key)
    if not host or not target:
        return []
    return [Mount(source=host, target=target, type="bind", read_only=True)]


# 作业配置 + JDBC 驱动。驱动因授权原因不随搬运镜像分发，缺了就是 ClassNotFoundException。
# 驱动逐个 jar 挂成文件——整目录挂会遮住工具自己的 lib。
_JOB_MOUNTS = _bind("jobs_host_dir", "jobs_mount") + [
    Mount(source=j["source"], target=j["target"], type="bind", read_only=True)
    for j in _SPEC.get("driver_jars") or []
]


class _SyncOperator(DockerOperator):
    """DockerOperator，但不把 command 里的 .sh 当模板文件。

    DockerOperator 的 ``template_ext`` 默认含 ``.sh``/``.bash``：凡是渲染时以这些后缀
    结尾的字符串，Airflow 会当成**要从 DAG 目录加载的模板文件**去读。搬运命令的第一个
    元素是容器内的 ``…/seatunnel.sh`` 路径（一个参数，不是模板），于是每个搬运任务都会
    在渲染期炸「Exception rendering Jinja template … field 'command'」。
    清空 ``template_ext`` 只关掉「从文件读模板」，``command`` 仍在 ``template_fields``
    里，``{{{{ data_interval_start }}}}`` 照常渲染。
    """

    template_ext = ()


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
        op = _SyncOperator(
            task_id=task["task_id"],
            image=task["image"],
            # 镜像与命令由搬运工具（seatunnel/datax/flink）的 Adapter 产出；
            # command 是 DockerOperator 的模板字段，里面的 {{{{ data_interval_start }}}}
            # 由 Airflow 在运行时渲染为本次数据区间起点（水位），补数自动回区间。
            command=task["command"],
            # 凭据：值是 {{{{ conn.… }}}} 表达式，environment 是模板字段，Airflow 在
            # 任务运行时才解析 Connection。DAG 与 spec 里因而不含任何账号密码。
            environment=task["env"],
            # providers-docker 的参数名是单数形式；写错的话 DockerOperator 会在
            # **解析期**抛 AirflowException，整个 DAG 目录导入失败。
            mount_tmp_dir=False,
            auto_remove="success",
            # 搬运容器要能解析源库/目标仓的容器名，默认 bridge 网络做不到。
            network_mode=_SPEC["docker_network"],
            # 作业配置挂只读进容器；不挂的话 --config 指向的文件在容器里根本不存在。
            mounts=_JOB_MOUNTS,
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
