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
    """一次物化的全部产物。三个文件都要落盘、都可 diff。

    ``runner_jar_*`` 描述随包分发的 SqlRunner：``runner_jar_path`` 是 ontoMeta 侧的
    源文件（投递器读它的字节），``runner_jar_filename`` 是内容寻址后的目标文件名
    （``sql-runner-<sha12>.jar``）。两者为空表示这次不带 jar（handoff 模式）。
    """

    dag_id: str
    dag_filename: str
    dag_source: str
    spec_filename: str
    spec: dict
    runner_jar_path: str = ""  # ontoMeta 侧 jar 源路径
    runner_jar_filename: str = ""  # 内容寻址后的文件名（含 sha12）


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
    """Flink 提交的基础配置（SqlRunner JAR、YARN 队列、checkpoint 目录）。

    字段与两处调用方（flink_job_runner / materialization_runner）对齐：
    runner_jar 是通用 SqlRunner JAR（读 SQL 文件、替换占位符后逐条 executeSql），
    runner_class 是其 main class，flink_bin 是 flink 命令路径。

    **runner_jar 是 ontoMeta 侧的本地路径**：投递时读它的字节、按 sha256 内容寻址随包
    发到 Airflow 主机的 ``ontometa/_lib/``，DAG 运行期再用 xcom 算出的 lib_dir 拼绝对
    路径。此前它是"Airflow 机器上的绝对路径"，ontoMeta 从不打开它——那要求 jar 预先
    摆在 Airflow 机器上并手工保持版本一致，与"执行信息全在 ontoMeta"相悖。
    """

    runner_jar: str  # SqlRunner JAR 路径（**ontoMeta 侧本地路径**，投递时随包分发）
    runner_class: str = "com.ontometa.flink.SqlRunner"  # SqlRunner main class
    flink_bin: str = "flink"  # flink 命令路径
    deploy_target: str = "yarn-per-job"  # flink run 部署目标
    parallelism: int = 1  # 默认并行度
    yarn_queue: str = "default"  # YARN 队列
    extra_args: tuple[str, ...] = ()  # 透传给 flink run 的额外参数（如 -Dkey=val）
    checkpoint_dir: str = ""  # checkpoint 目录（file://… 或 hdfs://…），增量/CDC 需要


@dataclass(frozen=True)
class FlinkSqlTask:
    """一个 Flink SQL 搬运任务的完整声明（SQL + 端点凭据占位符 + 提交参数）。

    L1（血缘）：``source_urns`` / ``target_urn`` 是 Dataset URN（``库.表`` 经
    ``datahub.build_dataset_urn`` 构造）。由调用方（executor / move_job_compiler）
    填充——可从 SQL 解析（A1，见 ``flink_sql_lineage``）或直接用已算好的物理名。
    Airflow DataHub 插件据此自动上报任务级血缘（inlets/outlets）。
    """

    task_id: str
    sql: str  # 完整 Flink SQL（INSERT INTO … SELECT …），含 ${} 占位符
    env: dict[str, str] = field(default_factory=dict)  # 端点凭据环境变量
    detached: bool = False  # 流式作业（incremental/cdc）需 -d
    checkpoint_dir: str = ""  # 流式作业的 checkpoint 目录
    # ---- L1 新增：血缘（inlets / outlets）----
    source_urns: tuple[str, ...] = ()  # 源表 URN（inlets）
    target_urn: str = ""  # 目标表 URN（outlets）


def flink_dag_id_for(base: str, suffix: str | None = None) -> str:
    """稳定的 Flink 计算 DAG id：``ontometa_flink_<short>``[__suffix]。

    ``base`` **取制品 id**（缺失时才退本体 id）；short 取去横线后前 12 字符，保证
    DAG id 稳定、可读、且天然带 flink 作用域前缀（与 transform/metric 的 Flink DAG
    同命名空间）。

    以本体 id 为 base 会撞号：同一本体上物化与同步是两条并存的制品，cron 后缀又相同，
    两者会算出同一个 dag_id 并互相覆盖投递的文件——建表 DAG 与搬运 DAG 谁后投递谁生效。
    产物目录早已按 artifact_id 隔离，dag_id 跟上即可。
    """
    short = (base or "").replace("-", "")[:12]
    dag_id = f"ontometa_flink_{short}"
    return f"{dag_id}__{suffix}" if suffix else dag_id


def runner_jar_identity(jar_path: str) -> tuple[str, str]:
    """读 jar 算内容寻址身份，返回 ``(目标文件名, sha256)``。

    文件名带 sha256 前 12 位：同一版本只有一份、多个制品共享，升级 jar 得到新文件名故
    老制品完全不受影响（重跑三个月前的作业不会因为 runner 变了而结果不同）。

    Raises:
        FileNotFoundError / OSError: jar 读不到——调用方应转成面向用户的报错，说清
            这是 **ontoMeta 侧**的路径（它以前是 Airflow 侧路径，语义变了）。
    """
    with open(jar_path, "rb") as fh:
        digest = hashlib.sha256(fh.read()).hexdigest()
    return f"sql-runner-{digest[:12]}.jar", digest


def _flink_run_command(
    task: FlinkSqlTask, config: FlinkSubmitConfig, sql_file: str, jar_file: str
) -> str:
    """拼 `flink run` 命令行（返回字符串，直接进 BashOperator 的 bash_command）。

    SqlRunner 只认 ``--file <path>``（见 tools/flink-sql-runner/SqlRunner.java），
    故 sql 路径必须走 ``--file``，不能作位置参数。sql_file 与 jar_file 都是运行期解析的
    Jinja 表达式（xcom_pull 的 sql_dir / lib_dir + 文件名），生成期不写死绝对路径——
    ontoMeta 与 Airflow 不在同一台机器时，生成期根本不知道远端挂载点。
    """
    parts = [config.flink_bin, "run", "-t", config.deploy_target, "-p", str(config.parallelism)]
    if task.detached:
        parts.append("-d")  # 流式作业 detached 运行
    if config.yarn_queue:
        parts.append(f"-Dyarn.application.queue={config.yarn_queue}")
    parts.extend(config.extra_args)
    parts.extend(["-c", config.runner_class, jar_file, "--file", sql_file])
    command = " ".join(parts)
    if not task.detached:
        return command
    # BashOperator pushes its final stdout line to XCom. Normalize Flink's
    # detached submission text into JSON and fail if no real JobID is present.
    # Never substitute DagRun/task ids for the Flink runtime identity.
    return (
        "set -o pipefail; "
        f"_ontometa_out=$({command} 2>&1); "
        "_ontometa_rc=$?; printf '%s\\n' \"$_ontometa_out\"; "
        "[ $_ontometa_rc -eq 0 ] || exit $_ontometa_rc; "
        "_ontometa_job_id=$(printf '%s\\n' \"$_ontometa_out\" | "
        "sed -nE 's/.*(JobID|Job ID)[^0-9a-fA-F]*([0-9a-fA-F]{16,64}).*/\\2/p' | tail -1); "
        "[ -n \"$_ontometa_job_id\" ] || "
        "{ echo 'Flink detached submission returned no JobID' >&2; exit 42; }; "
        "printf '{\"flink_job_id\":\"%s\"}\\n' \"$_ontometa_job_id\""
    )


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
    task_dependencies: list[tuple[FlinkSqlTask, FlinkSqlTask]] | None = None,
    dag_id_base: str | None = None,
    nested_layout: bool = True,
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
    - ``task_dependencies``：**L3 血缘驱动的任务间依赖**（(上游, 下游) 边列表）。
      非空时 DAG 里按这些边串 ``上游 >> 下游``（Airflow 默认 all_success——
      上游失败时下游不执行）。所有任务仍依赖 create_tables（建表前置不变）。
      空 = 现有行为（全部任务只依赖 create_tables，彼此无依赖）。
    - ``dag_id_base``：算 dag_id 用的 base，缺省取 ``ontology_id``。调用方应传**制品 id**
      ——同一本体上并存的多条制品（物化 / 同步）否则会算出同一个 dag_id 互相覆盖。
    - ``nested_layout``：产物是否落在 ``<dags>/ontometa/<artifact_id>/`` 子目录（默认是）。
      调用方无 artifact_id 时退回扁平 ``dags_dir``，此时须传 False——否则 DAG 运行期
      算 lib_dir 会往上越过 dags 目录。

    **DAG 结构**::

        read_spec ──> create_tables ──> [搬运任务…] ──> swap_<task> ──> _tails (仅全量表有 swap)
                                    ├──> [增量/CDC 任务…] (无 swap，直接 INSERT)
                                    └──> add_constraints (外键/主键，所有表建完后加)

    - ``create_tables``：跑 M3 生成的 DDL。**建表绝不交给搬运工具**。
      ``ddl_statements`` 为空时**不建这个算子**——只搬数据的同步 DAG（全增量、或关了
      staging）没有任何 DDL，硬建会得到一个 SQL 为空列表的算子。
    - 搬运任务：BashOperator 跑 `flink run`，SQL 文件从边车 JSON 读。
      它们都依赖 ``read_spec``：bash_command 里的 sql 路径是 ``xcom_pull(read_spec)``，
      不挂这条边时 Airflow 会与 read_spec 并发调度，拉到 None 拼出 ``None/xxx.sql``。
    - ``swap_<task>``：staging 切换任务（RENAME staging → 正式表）。
    - ``_tails``：虚拟任务，等所有 swap 完成（下游依赖挂这里）。
    - ``add_constraints``：加外键/主键（依赖 create_tables，所有表建完后跑）。

    **为什么没有任务分层**：Flink SQL 已处理依赖（源表 JOIN），不需要 dim → dwd 的硬排序。
    只保证「先建表、再搬运、swap 在搬完后、约束最后加」。

    **Staging swap**：全量表写 staging、原子切换；增量/CDC 直接写正式表（APPEND）。
    swap 的 SQL 由 materialization_runner._plan_staging 产出，本函数只负责编排。
    """
    dag_id = flink_dag_id_for(dag_id_base or ontology_id, dag_id_suffix)

    # SqlRunner jar 随包分发：算内容寻址文件名，命令里用 lib_dir 的 xcom 表达式拼路径。
    # jar 读不到时不静默降级——命令里留一个指不到的路径，只会在 Airflow 里得到一句
    # "Could not find file"，排查得从 DAG 日志倒推回设置页。
    jar_filename, jar_sha256 = "", ""
    if config.runner_jar:
        try:
            jar_filename, jar_sha256 = runner_jar_identity(config.runner_jar)
        except OSError as exc:
            raise FileNotFoundError(
                f"SqlRunner jar 读不到：{config.runner_jar}（{exc}）。"
                "该路径需在 **ontoMeta 侧**可读——jar 现在随制品包投递到 Airflow 主机，"
                "不再要求预先摆在 Airflow 机器上。"
            ) from exc
    jar_expr = (
        f"{{{{ ti.xcom_pull(task_ids='read_spec')['lib_dir'] }}}}/{jar_filename}"
        if jar_filename
        else config.runner_jar
    )

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
                "endpoint_env": task.env,
                "command": _flink_run_command(
                    task,
                    config,
                    f"{{{{ ti.xcom_pull(task_ids='read_spec')['sql_dir'] }}}}/{sql_filename}",
                    jar_expr,
                ),
                "detached": task.detached,
                # L1 血缘：inlets = 源表 URN（source_urns），outlets = 目标表 URN。
                # Airflow DataHub 插件据此自动上报任务级血缘（DataFlow/DataJob/Dataset）。
                # 空则不上报（血缘是增强，缺失不阻断执行）。
                "inlets": list(task.source_urns),
                "outlets": [task.target_urn] if task.target_urn else [],
            }
        )

    # Staging swap 任务（全量表才有）
    swap_specs: dict[str, list[str]] = {}
    if swaps:
        for task_id, swap_sqls in swaps.items():
            swap_specs[task_id] = [_as_single_statement(s) for s in swap_sqls]

    # 边车 JSON spec
    # L3 血缘驱动的任务间依赖：task_dependencies 是 (上游, 下游) 边列表，
    # 按 task_id 落成字符串对，模板里 ``上游 >> 下游``。
    dep_pairs = []
    if task_dependencies:
        ids = {t.task_id for t in tasks}
        for up, down in task_dependencies:
            if up.task_id not in ids or down.task_id not in ids:
                raise ValueError(
                    f"task_dependencies 引用了不在 tasks 里的任务："
                    f"{up.task_id} -> {down.task_id}"
                )
            dep_pairs.append((up.task_id, down.task_id))

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
        # L3：血缘驱动的任务间依赖（task_id 对）
        "task_dependencies": dep_pairs,
        "flink_config": {
            "runner_jar": config.runner_jar,
            "runner_class": config.runner_class,
            "flink_bin": config.flink_bin,
            "deploy_target": config.deploy_target,
            "parallelism": config.parallelism,
            "yarn_queue": config.yarn_queue,
            "checkpoint_dir": config.checkpoint_dir,
        },
        # 随包分发的 SqlRunner：文件名内容寻址、sha256 钉死版本。DAG 运行期据此
        # 拼 jar 绝对路径（配合 read_spec 吐的 lib_dir）。
        "runner": {
            "jar": jar_filename,
            "class": config.runner_class,
            "sha256": jar_sha256,
        },
    }

    # DAG 源码（Python 模板）
    # lib_dir 的运行期表达式：制品目录（<dags>/ontometa/<artifact_id>/）时 jar 在兄弟
    # 目录 ../\_lib；无 artifact_id 退回扁平 dags_dir 时往上两级会越界，故那时就用同级
    # 的 _lib。两种布局都由投递器保证真实存在（见 SshDelivery._deliver_lib）。
    lib_dir_expr = (
        'str(_SPEC_PATH.parent.parent / "_lib")'
        if nested_layout
        else 'str(_SPEC_PATH.parent / "_lib")'
    )
    dag_source = _render_flink_dag_source(dag_id, f"{dag_id}.json", lib_dir_expr)
    dag_filename = f"{dag_id}.py"
    spec_filename = f"{dag_id}.json"

    return DagBundle(
        dag_id=dag_id,
        dag_filename=dag_filename,
        dag_source=dag_source,
        spec_filename=spec_filename,
        spec=spec,
        runner_jar_path=config.runner_jar if jar_filename else "",
        runner_jar_filename=jar_filename,
    )


# DAG 文件模板。**刻意不是 f-string**：模板体里满是 Python 字典/f-string 的花括号，
# 放进 f-string 就得手工加倍转义，而加倍是数不清的——此前 ``default_args = {{{{…}}}}``
# 渲染出的是 ``{{…}}``（一个装着 dict 的 set，import 即 TypeError），
# ``f"swap_{{{{task_id}}}}"`` 渲染出的是字面量 ``swap_{{task_id}}``（多个 swap 撞同名任务）。
# 改成普通字符串 + 两个哨兵占位符后，花括号一律是它字面的意思，转义这一整类问题消失。
_FLINK_DAG_TEMPLATE = """\
# Flink SQL DAG: @@DAG_ID@@
# 统一执行架构：搬运走 Flink SQL on YARN（与 transform/metric 同一执行路径）
# 产物：本 DAG 文件 + 边车 JSON（@@SPEC_FILENAME@@）
# 本文件由 ontoMeta 生成，请勿手工编辑（改动会在下次投递时被覆盖）

from airflow import DAG
from airflow.operators.python import PythonOperator
from airflow.operators.bash import BashOperator
from airflow.providers.common.sql.operators.sql import SQLExecuteQueryOperator
from airflow.utils.task_group import TaskGroup
from datetime import datetime, timedelta
import json
from pathlib import Path

# 读边车 JSON spec
_SPEC_PATH = Path(__file__).parent / "@@SPEC_FILENAME@@"
_SPEC = json.loads(_SPEC_PATH.read_text(encoding="utf-8"))

default_args = {
    "owner": "ontoMeta",
    "depends_on_past": False,
    "email_on_failure": False,
    "email_on_retry": False,
    "retries": 1,
    "retry_delay": timedelta(minutes=5),
}

with DAG(
    dag_id="@@DAG_ID@@",
    default_args=default_args,
    description=f"Flink SQL 搬运：{_SPEC['ontology_id']}",
    schedule=_SPEC.get("schedule"),
    start_date=datetime(2024, 1, 1),
    catchup=False,
    max_active_tasks=_SPEC.get("max_active_tasks", 16),
    tags=["ontometa", "flink", "sync"],
) as dag:

    # 1. 读 spec，暴露 sql_dir / lib_dir 给下游
    #    .sql 落 spec 同级的 jobs/；SqlRunner jar 落 @@LIB_DIR_EXPR@@（制品目录的兄弟
    #    目录 ontometa/_lib，跨制品共享）。lib_dir 由生成期算好写死在这里而非用
    #    ``.parent.parent`` 推：无 artifact_id 时产物退回扁平 dags_dir，那时往上两级
    #    会跑到 dags 目录之外。
    def _read_spec(**context):
        return {
            "spec": _SPEC,
            "sql_dir": str(_SPEC_PATH.parent / "jobs"),
            "lib_dir": @@LIB_DIR_EXPR@@,
        }

    read_spec = PythonOperator(
        task_id="read_spec",
        python_callable=_read_spec,
    )

    # 2. 建表（M3 生成的 DDL，**绝不交给搬运工具自动建**）
    #    只搬数据的同步 DAG 可能一条 DDL 都没有（全增量、或关了 staging）——那时不建这个
    #    算子，否则得到一个 sql=[] 的空 SQL 任务（连着目标仓、什么也不做）。
    warehouse_ddl = _SPEC.get("warehouse_ddl") or []
    create_tables = None
    if warehouse_ddl:
        create_tables = SQLExecuteQueryOperator(
            task_id="create_tables",
            conn_id=_SPEC["warehouse_conn_id"],
            sql=warehouse_ddl,
            split_statements=True,
        )
        read_spec >> create_tables

    # 3. 搬运任务（BashOperator 跑 flink run）
    move_tasks = {}
    for task_spec in _SPEC["tasks"]:
        # 端点凭据从 Airflow Connection 读，注入环境变量（$别名_URL / $别名_USER 等）
        env_vars = task_spec.get("endpoint_env", {})
        # flink run 命令（生成期已拼成字符串，含 --file 的 xcom 路径表达式）
        cmd = task_spec["command"]
        move_task = BashOperator(
            task_id=task_spec["task_id"],
            bash_command=cmd,
            env=env_vars,
            # append_env 保留 worker 自身环境（PATH / HADOOP_CONF_DIR / FLINK_HOME）。
            # Airflow 的 env 默认**替换**而非追加整个环境——不加这个，flink_bin 若配的是
            # 裸 `flink` 就会 command not found。
            append_env=True,
            # L1 血缘：inlets/outlets 让 DataHub 插件自动上报任务级血缘。
            # 空列表（未解析到）时 Airflow 忽略，不阻断。
            inlets=task_spec.get("inlets", []),
            outlets=task_spec.get("outlets", []),
        )
        move_tasks[task_spec["task_id"]] = move_task
        # sql 路径来自 read_spec 的 XCom：不挂这条边，Airflow 会与 read_spec 并发调度，
        # 拉到 None 拼出 "None/xxx.sql"。此前靠 create_tables 间接排在其后，而只搬数据的
        # DAG 没有建表任务。
        read_spec >> move_task
        # 搬运依赖建表（本 DAG 有建表任务时）
        if create_tables is not None:
            create_tables >> move_task

    # 3.5 L3 血缘驱动的任务间依赖：上游产出表 → 下游消费表（Airflow all_success，
    # 上游失败时下游不执行）。task_dependencies 是 (上游task_id, 下游task_id) 对。
    for up_id, down_id in _SPEC.get("task_dependencies", []):
        move_tasks[up_id] >> move_tasks[down_id]

    # 4. Staging swap（全量表才有，增量/CDC 无）
    swaps = _SPEC.get("swaps", {})
    swap_tasks = {}
    for task_id, swap_sqls in swaps.items():
        swap_task = SQLExecuteQueryOperator(
            task_id=f"swap_{task_id}",
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
        # 约束只随建表产出，故正常必有 create_tables；防御性地兜住没有的情形。
        (create_tables or read_spec) >> add_constraints
"""


def _render_flink_dag_source(
    dag_id: str, spec_filename: str, lib_dir_expr: str
) -> str:
    """渲染 Flink DAG 的 Python 源码（Airflow DAG 文件）。

    DAG 文件只有骨架（任务怎么连、用什么 Operator），SQL / DDL 从边车 JSON 读。
    这样 DAG 文件稳定（可 review）、JSON 可 diff（734 张表的 SQL 不内联进 .py）。

    ``lib_dir_expr`` 是一段**Python 表达式源码**（不是路径值），原样嵌进模板由
    DAG 运行期求值——jar 的位置只有在 Airflow 主机上才算得准。

    **只能用 str.replace**：模板里满是字面花括号，走 .format() / f-string 就得手工
    加倍转义，那正是当年那批「能 parse、一 import 就炸」产物的成因（见模板上方注释）。
    """
    return (
        _FLINK_DAG_TEMPLATE.replace("@@DAG_ID@@", dag_id)
        .replace("@@SPEC_FILENAME@@", spec_filename)
        .replace("@@LIB_DIR_EXPR@@", lib_dir_expr)
    )
