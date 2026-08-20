"""本体 → Airflow 的编排入口：**物化**（建结构）与**数据同步**（搬数据）两条产出。

本体有两种来源，对应两件不同的事：

- **人工建模**的对象任何库里都没有物理表，必须先建出来给业务用 → ``run_materialize``，
  只产建表 DDL（含外键），DAG 是 ``read_spec → create_tables``、``schedule=None`` 的
  一次性任务。**不产任何搬运作业、不产 staging/swap，一行数据都不动。**
- **DataHub 采集**的对象源头数据已存在，要搬进数仓 → ``run_sync``，只产 Flink SQL 搬运
  作业（含全量的 staging + 原子切换）。**不建业务表**——目标表必须已由物化建好，
  这不只是策略：staging 的 ``CREATE TABLE … LIKE <正式表>`` 结构上就要求它先存在。

两条共用 ``_orchestrate``（目标源校验、契约对齐、DDL 生成、投递、触发），差别只在
``emit``。**刻意不做成单个 ``run(emit=...)``**：``load_strategy``/``refresh_cron`` 只对同步
有意义，合成一个签名就会得到一串「只在 emit=X 时才有意义」的形参——本仓已被这个形状
坑过两次（见 ``run_sync`` 的 ``load_strategy`` 说明）。

本模块把已有能力串起来，不重造任何一块：

- **物化契约**（``services/materialization_contract``）：先 ``sync`` 保证契约存在/最新，
  弹窗覆盖的存储策略/表名作为 override 写回并钉住，使“生成”与“展示”同一事实源。
- **正向生成器**（``services/warehouse_generator``）：按目标引擎产出建表 DDL（本体反补的
  注释/分区/主键声明只在这条路径上）。
- **搬运作业编译**（``services/job_planner`` + ``app/warehouse/jobs``）：本体+契约 → JobSpec
  → Flink SQL 作业。
- **DAG 生成与投递**（``services/airflow_dag_builder`` + ``connectors/airflow``）：产物落盘交
  Airflow，触发一次 DagRun。落库由 Airflow 按 conn_id 执行，ontoMeta 不直连目标库。
"""

from __future__ import annotations

import hashlib
import os
import time
from typing import Any, Literal

from sqlalchemy.orm import Session

from app.connectors.airflow import AirflowClient, AirflowError
from app.connectors.datahub import build_dataset_urn
from app.models.data_app import DataSource
from app.services.airflow_dag_builder import (
    _plan_staging,
    build_flink_sql_dag,
)
from app.services.job_planner import JobPlanner
from app.services.materialization_contract import MaterializationContractService
from app.services.move_job_compiler import compile_move_task
from app.services import flink_params
from app.services.settings_service import SettingsService
from app.services.warehouse_generator import WarehouseGenerator
from app.warehouse import DEFAULT_ENGINE, list_engines
from app.warehouse.jobs import JobPlan

# 统一执行架构：搬运一律走 Flink SQL on YARN（与 transform/metric 同一执行路径），
# 不再有 seatunnel/datax/docker/runner 多通道。工具恒为 flink。
_SYNC_TOOL = "flink"

# 本次编排产出什么：``ddl`` = 只建结构（物化），``dml`` = 只搬数据（同步）。
Emit = Literal["ddl", "dml"]

_contract_service = MaterializationContractService()
_generator = WarehouseGenerator()
_job_planner = JobPlanner()
_settings = SettingsService()


def resolve_engine(
    db: Session, target_datasource_id: str | None, spec_engine: Any = None
) -> str:
    """物化引擎（DDL/ETL 方言）的唯一推定口径。

    引擎信息就在目标数据源里（``DataSource.kind``），不再让用户/表单单独选。
    与前端 ``MaterializeModal.engineOfKind`` 同口径：数据源 kind 命中已注册引擎即用它，
    否则回退 ``DEFAULT_ENGINE``。显式传入的 ``spec_engine``（旧制品/对话链路）优先。
    """
    if spec_engine:
        return str(spec_engine)
    ds = db.get(DataSource, target_datasource_id) if target_datasource_id else None
    if ds is not None:
        key = (ds.kind or "").lower()
        if key in set(list_engines()):
            return key
    return DEFAULT_ENGINE


class MaterializationError(ValueError):
    """物化前置条件错误（目标源不存在 / 未配置连接串等），面向用户可读。"""


def _warehouse_conn_id(ds: DataSource) -> str:
    """目标仓的 Airflow Connection id，由目标数据源推导。

    目标仓就是弹窗里选的 target DataSource，故 conn_id 与数据源 1:1（不同目标需不同
    连接），不再放全局设置。部署方在 Airflow 建一个此 id 的 Connection 指向该仓即可。
    """
    slug = "".join(c if c.isalnum() else "_" for c in (ds.name or ds.id)).strip("_").lower()
    return f"ontometa_ds_{slug or ds.id[:8]}"


def _bare_name(qualified: str) -> str:
    """``dim_erp.customer`` → ``customer``。generator 的 statements 以库.表为键。"""
    return qualified.split(".")[-1]


def _select(
    statements: dict[str, str], selected: set[str] | None
) -> list[tuple[str, str]]:
    """按用户勾选裁剪，保持 generator 的稳定顺序，返回 (qualified, sql) 列表。

    ``selected`` 为实体名集合；None 表示不裁剪（全选）。
    """
    items = list(statements.items())
    if selected is None:
        return items
    return [(q, s) for q, s in items if _bare_name(q) in selected]


def _select_constraints(
    constraints: dict[str, list[dict]] | None,
    ddl_items: list[tuple[str, str]],
) -> dict[str, list[dict]]:
    """把外键语句裁到「本次真的会建的表」之内，返回 {表 → [{ref, sql}]}。

    两头都要在范围内：**约束所在的表**要建，**被引用的表**也要建。物化一部分是常规
    用法（弹窗就支持勾选），而 postgres 加外键时会校验被引用表存在——只按前者裁的话，
    勾了子表没勾父表，加约束那步整批红。

    ``ref`` 一路留到分批（``_assign_ddl``）之后才丢：跨批判定还要用它。

    这里丢掉的约束不是错误：补物化被引用表后重跑，DO 块是幂等的，约束自然补上。
    """
    if not constraints:
        return {}
    in_scope = {q for q, _ in ddl_items}
    out: dict[str, list[dict]] = {}
    for qualified in (q for q, _ in ddl_items):
        kept = [c for c in constraints.get(qualified) or [] if c.get("ref") in in_scope]
        if kept:
            out[qualified] = kept
    return out


def _selected_names(
    db: Session,
    ontology_id: str,
    selected_targets: list[str] | None,
    table_overrides: dict[str, str] | None,
) -> set[str] | None:
    """勾选的实体名 → 裁剪用的物理表名集合。

    弹窗按实体名勾选，而人工改过表名的实体在 generator 里已用新名，二者对不上会被
    误裁掉，故把这些实体的新表名一并纳入。
    """
    if not selected_targets:
        return None
    names = set(selected_targets)
    if not table_overrides:
        return names
    contracts = [
        c
        for c in _contract_service.list_contracts(db, ontology_id)
        if c.id in table_overrides
    ]
    resolved = _contract_service.resolve_target_names(db, contracts)
    for contract in contracts:
        entity_name = (resolved.get(contract.target_id) or (None, None))[0]
        if entity_name is None or entity_name in names:
            names.add(table_overrides[contract.id])
    return names


def _cron_by_entity(db: Session, ontology_id: str) -> dict[str, str | None]:
    """本体实体名 → 契约的 refresh_cron（空串归一为 None）。M16 按此分组。"""
    contracts = _contract_service.list_contracts(db, ontology_id, materialized_only=True)
    names = _contract_service.resolve_target_names(db, contracts)
    out: dict[str, str | None] = {}
    for c in contracts:
        entity = (names.get(c.target_id) or (None,))[0]
        if entity:
            out[entity] = (c.refresh_cron or "").strip() or None
    return out


def _cron_suffix(cron: str | None) -> str:
    """cron → DAG id 后缀。无 cron 归 ``manual``；有 cron 用短哈希（同 cron 稳定同后缀）。"""
    if not cron:
        return "manual"
    return "c" + hashlib.md5(cron.encode("utf-8")).hexdigest()[:8]


def _plan_batches(jobs, cron_by_entity: dict[str, str | None], max_tasks: int) -> list[dict]:
    """按 cron 分组、组内按 max_tasks 分批 → 每个 (后缀, schedule, 作业子集)。

    一个 cron 一个 DAG（少数派 cron 不再被众数吞掉）；单组超 max_tasks 再拆成
    ``_b0``/``_b1``… 多个 DAG。作业已按 (layer, name) 稳定排序，分批结果因而幂等。
    """
    groups: dict[str | None, list] = {}
    for job in jobs:
        cron = cron_by_entity.get(job.entity_name)
        groups.setdefault(cron, []).append(job)

    batches: list[dict] = []
    # 稳定顺序：cron 字符串升序，无 cron（manual）排最后。
    for cron in sorted(groups, key=lambda c: (c is None, c or "")):
        group_jobs = groups[cron]
        base = _cron_suffix(cron)
        chunks = [
            group_jobs[i : i + max_tasks] for i in range(0, len(group_jobs), max_tasks)
        ]
        multi = len(chunks) > 1
        for i, chunk in enumerate(chunks):
            batches.append(
                {
                    "suffix": f"{base}_b{i}" if multi else base,
                    "schedule": cron,
                    "jobs": tuple(chunk),
                }
            )
    return batches


def _assign_ddl(
    batches: list[dict],
    ddl_items: list[tuple[str, str]],
    constraint_items: dict[str, list[str]] | None = None,
) -> list[dict]:
    """把建表语句分给各批，并保证**每张表都有人建**。

    有搬运作业的表，建表跟着它那一批走。**没有任何搬运作业的表也必须建**——ADS 层
    （由 MetricSpec 产出，不走搬运）、缺 ``source_ref``、装载方式不被执行侧支持的，
    都属这一类。按批内作业的目标表筛 DDL 会把它们整个漏掉：表出现在回执的 ``tables``
    里，却没有任何 DAG 会创建它。故这些「孤儿 DDL」归入 manual 批，没有就新开一个
    只建表的批。

    **不按 cron 分**：建表是幂等的一次性动作，挂到定时 DAG 上只会每轮重复跑一遍
    CREATE，而这些表本来就没有「按节奏重跑」的语义。
    """
    by_target = dict(ddl_items)
    assigned: set[str] = set()
    for batch in batches:
        targets = {job.target.qualified for job in batch["jobs"]}
        batch["ddl"] = {q: by_target[q] for q in sorted(targets & set(by_target))}
        assigned |= targets

    orphans = {q: s for q, s in ddl_items if q not in assigned}
    if orphans:
        manual = next((b for b in batches if b["schedule"] is None), None)
        if manual is None:
            manual = {"suffix": "manual", "schedule": None, "jobs": (), "ddl": {}}
            batches.append(manual)
        manual["ddl"].update(orphans)

    # 外键（两段式 DDL 的第二段）跟着**约束所在的表**那一批走，且只保留被引用表也在
    # 同一批的那些：各批是彼此独立的 DAG、同时触发、互相之间没有先后，跨批引用就可能
    # 在被引用表建出来之前执行。跨批被丢掉的记进 batch["constraints_deferred"]，由回执
    # 报出去——补物化后重跑会补上（DO 块幂等）。
    owner = {q: i for i, b in enumerate(batches) for q in (b.get("ddl") or {})}
    for i, batch in enumerate(batches):
        kept: dict[str, list[str]] = {}
        deferred: list[str] = []
        for qualified in sorted(batch.get("ddl") or {}):
            for item in (constraint_items or {}).get(qualified) or []:
                if owner.get(item["ref"]) == i:
                    kept.setdefault(qualified, []).append(item["sql"])
                else:
                    deferred.append(f"{qualified} → {item['ref']}")
        batch["constraints"] = kept
        batch["constraints_deferred"] = deferred
    return batches


def _wait_for_parse(
    client: AirflowClient, dag_ids: list[str], timeout: float
) -> set[str]:
    """等 Airflow 解析到这批 DAG，返回**已解析到的** dag_id 集合。

    替代「落盘后立刻触发、404 被吞成回执 error」：首次提交常见于解析尚未完成，
    直接触发必 404。⚠ Airflow ``dag_dir_list_interval`` 默认 300s（§8.1），超时值
    由 ``ONTOMETA_DAG_PARSE_TIMEOUT`` 配；超时不抛，交由调用方记「尚未解析到」。

    **一个总超时管整批，不是每个各等一遍**：这批文件在同一次写盘里全部落到同一个
    dags 目录，Airflow 一次目录扫描就会全部认到——逐个各等 ``timeout`` 并不会让谁
    更容易被认到，只会在目录两侧不一致（失败模式 #3，正是这里要抓的那个）时把
    等待放大成「批数 × 超时」：734 张表约 15 批 × 60s ≈ 15 分钟阻塞在一个 HTTP
    请求里，网关多半先超时，用户什么也拿不到。
    """
    deadline = time.monotonic() + timeout
    pending = list(dag_ids)
    seen: set[str] = set()
    while pending:
        for dag_id in list(pending):
            try:
                if client.dag_exists(dag_id):
                    seen.add(dag_id)
                    pending.remove(dag_id)
            except AirflowError:
                pass  # 鉴权/网络问题由触发那步的错误体带出，这里只管「在不在」
        if not pending or time.monotonic() >= deadline:
            break
        time.sleep(2)
    return seen


def _run_orchestrated(
    db: Session,
    ontology_id: str,
    *,
    emit: Emit,
    ds: DataSource,
    engine: str,
    airflow,
    ddl_items: list[tuple[str, str]],
    constraint_items: dict[str, list[str]],
    database_prefix: str | None,
    database_overrides: dict[str, str] | None,
    table_overrides: dict[str, str] | None,
    selected_targets: list[str] | None,
    artifact_id: str | None,
    load_strategy: str | None = None,
    flink_task_params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """产出 DAG → 投递 → 触发一次运行。**不在本进程里落库**。

    ``emit="ddl"``（物化）：不调 ``JobPlanner``，``plan`` 恒为空。全部 DDL 经 ``_assign_ddl``
    归进合成的 manual 批，产出一条 ``read_spec → create_tables [→ add_constraints]``、
    ``schedule=None`` 的 DAG。**不校验 Flink SqlRunner JAR**——``create_tables`` 是
    ``SQLExecuteQueryOperator``（直连目标仓的 Airflow Connection），与 Flink 无关。此前
    在建 bundle 之前就因缺 JAR 退 handoff，于是没装 SqlRunner 的部署上人工建模的本体
    拿到 ``ok: True`` 却一张表都没建。

    ``emit="dml"``（同步）：``ddl_items``/``constraint_items`` 由调用方置空，批次只由搬运
    作业分组产生，回执的 ``tables`` 因而为空（前端靠它判「已物化」，同步填了就会冒充
    成物化）。批里的 DDL 只可能是 staging 表（``CREATE TABLE … LIKE 正式表``，属搬运侧）。

    统一执行架构：搬运一律编译成 Flink SQL、经 Airflow BashOperator ``flink run`` 提交到
    YARN（与 transform/metric 同一路径）。
    """
    if emit == "dml":
        # 搬运工具恒为 flink。planner 用 FlinkAdapter 判可搬性（full/incremental/cdc 均支持），
        # runner_capabilities 传 None（那套是 sync-runner 通道的遗留，已废）。
        plan = _job_planner.build(
            db,
            ontology_id,
            engine=engine,
            tool=_SYNC_TOOL,
            target_alias=_warehouse_conn_id(ds),
            database_prefix=database_prefix,
            database_overrides=database_overrides,
            table_overrides=table_overrides,
            selected_targets=selected_targets,
            runner_capabilities=None,
            # 本次运行的装载方式覆盖（Spec 里选的「全量/增量」），缺省 None = 逐表按契约。
            load_strategy=load_strategy,
        )
        if not plan.jobs:
            # 一个作业都编不出来 = 没有任何数据会被搬。此前这里静默回 ok: True，
            # 而拆分后这是最可能的失败形态（选中的对象全是人工建模的，压根没有源表）。
            raise MaterializationError(_no_movable_objects_message(plan))
    else:
        plan = JobPlan()

    # Flink on YARN 提交参数（设置页 → Airflow/Flink，DB）。搬运缺 SqlRunner JAR → 退「仅产出」；
    # 建表不经 Flink，故物化不看这个。
    runner_jar = (airflow.flink_sql_runner_jar or "").strip()
    if emit == "dml" and not runner_jar:
        return _handoff_receipt(
            ontology_id, ds, engine, emit, plan, ddl_items,
            database_prefix, database_overrides, table_overrides,
        )
    # 设置页给默认值，**制品 Spec 逐任务覆盖**（并行度/队列/提交目标/checkpoint/额外 -D）：
    # 一条搬 300 张表的同步与一条小表 CDC 对集群资源的要求不是一回事。
    flink_cfg = flink_params.resolve_config(
        airflow, flink_task_params, runner_jar=runner_jar
    )
    checkpoint_dir = flink_cfg.checkpoint_dir or None
    warehouse_conn_id = _warehouse_conn_id(ds)

    # 按 cron 分组 + 按上限分批：一个 cron 一个 DAG、少数派不再被众数吞掉，超上限再拆（M16）。
    # 物化没有作业，也就没有可分组的 cron——建表是幂等的一次性动作，全部归 manual 批
    # （见 _assign_ddl），挂到定时 DAG 上只会每轮重复跑一遍 CREATE。
    cron_map = _cron_by_entity(db, ontology_id) if plan.jobs else {}
    batches = _plan_batches(plan.jobs, cron_map, airflow.max_tasks_per_dag)
    # 建表分配：有作业的表跟着自己那批，无作业的表（ADS/缺 source_ref）归入 manual 批。
    _assign_ddl(batches, ddl_items, constraint_items)

    # 有 artifact_id 时 .sql 落 <dags_dir>/ontometa/<artifact_id>/jobs/（与 flink run --file
    # 路径对齐）；空则退回扁平 jobs_dir。

    bundles: list[tuple[dict, Any]] = []
    for batch in batches:
        # 全量走 staging + 原子切换（M15）：搬到一半失败时正式表原封不动。_plan_staging 把
        # full 作业的 target.table 改写成 staging 名（保留 target_table），并给出建 staging
        # 表的 DDL 与切换语句。compile_move_task 跑改写后的 job → INSERT 自然进 staging 表。
        stage = _plan_staging(
            JobPlan(jobs=batch["jobs"]),
            engine=engine,
            token=batch["suffix"],
            enabled=airflow.staging_swap,
        )
        tasks = []
        swaps: dict[str, list[str]] = {}
        for job in batch["jobs"]:
            # staging 化的 job（full 表写 staging）或原 job（增量/未启用 staging）。
            exec_job = stage.exec_jobs.get(job.name, job)
            try:
                tasks.append(
                    compile_move_task(
                        exec_job, engine=engine, checkpoint_dir=checkpoint_dir
                    )
                )
            except ValueError as exc:
                # 编译期的搬运约束（如增量/CDC 缺 checkpoint_dir、源无 CDC 连接器）转成
                # 用户可读的物化错误，而不是让原始 ValueError 逃逸成 500。
                raise MaterializationError(
                    f"作业「{job.name}」无法编译成 Flink 搬运：{exc}"
                ) from exc
            if stage.swaps.get(job.name):
                swaps[job.name] = stage.swaps[job.name]

        # 建表 DDL = 正式表 + 外键约束 + staging 表（staging 建在正式表之后，CREATE LIKE 依赖它）。
        ddl: list[str] = list(batch["ddl"].values())
        for stmts in (batch.get("constraints") or {}).values():
            ddl.extend(stmts)
        ddl.extend(stage.ddl)

        if not tasks and not ddl:
            continue  # 空批（不该出现，防御）
        bundle = build_flink_sql_dag(
            ontology_id=ontology_id,
            engine=engine,
            tasks=list(tasks),
            ddl_statements={f"ddl_{i}": d for i, d in enumerate(ddl)},
            constraints=batch.get("constraints") or None,
            swaps=swaps,
            config=flink_cfg,
            schedule=batch["schedule"],
            dag_id_suffix=batch["suffix"],
            warehouse_conn_id=warehouse_conn_id,
            max_active_tasks=airflow.max_active_tasks_per_dag,
            # dag_id 以**制品**为 base：同一本体上物化与同步是两条并存的制品，以本体 id
            # 为 base 时两者算出同一个 dag_id，谁后投递谁覆盖谁。
            dag_id_base=artifact_id or ontology_id,
            # 无 artifact_id 时产物落扁平 dags_dir，DAG 里算 lib_dir 不能再往上一级
            nested_layout=bool(artifact_id),
        )
        bundles.append((batch, bundle))

    # 先全部落盘：等解析时一次 dag_dir 扫描即可全部认到，避免逐个各等一个解析周期。
    # DagBundle 是纯数据（F/G 后不再有 write 方法），交给统一的 DagDelivery 投递器
    # （SshDelivery：rsync 到 Airflow 主机后原子切换），SQL 作为 job_files 一并投。
    # 产物按 <dags>/ontometa/<artifact_id>/ 子目录聚合，.sql 落其 jobs/（与 read_spec 的
    # sql_dir 对齐）；无 artifact_id 时退回扁平 dags_dir。
    if artifact_id:
        _out_dir = os.path.join(airflow.dags_dir, "ontometa", artifact_id)
    else:
        _out_dir = airflow.dags_dir
    _jobs_dir = os.path.join(_out_dir, "jobs")
    written_all: dict[str, dict] = {}
    delivery = airflow.build_delivery()
    for _, bundle in bundles:
        job_files = {t["sql_file"]: t["sql"] for t in bundle.spec["tasks"]}
        # SqlRunner jar 随包投递（内容寻址，落远端 ontometa/_lib/，多制品共享一份）。
        # 同一批 bundle 用的是同一个 jar，rsync 增量会自动跳过重复传输。
        lib_files = {}
        if bundle.runner_jar_filename:
            with open(bundle.runner_jar_path, "rb") as fh:
                lib_files[bundle.runner_jar_filename] = fh.read()
        try:
            result = delivery.deliver(
                dags_dir=_out_dir,
                jobs_dir=_jobs_dir,
                dag_filename=bundle.dag_filename,
                dag_source=bundle.dag_source,
                spec_filename=bundle.spec_filename,
                spec=bundle.spec,
                job_files=job_files,
                lib_files=lib_files,
            )
        except Exception as exc:  # noqa: BLE001 —— 投递失败（含 OSError / DagDeliveryError）
            raise MaterializationError(f"DAG 投递失败：{exc}") from exc
        # 投递器给的是**远端**路径（前端「产物路径」面板展示它）。此前这里读的是
        # 不存在的 result.written，恒为 {} —— 面板永远是空白的。
        written_all[bundle.dag_id] = result.files_written

    client = AirflowClient(
        airflow.endpoint,
        username=airflow.username,
        password=airflow.password,
    )
    batch_results: list[dict] = []
    parse_timeout = airflow.dag_parse_timeout
    try:
        # 一个总超时管整批（见 _wait_for_parse）：它们在同一次写盘里落到同一个目录，
        # 一次扫描全部认到；逐个各等一遍只会在目录不可见时把等待乘以批数。
        parsed = _wait_for_parse(
            client, [bundle.dag_id for _, bundle in bundles], parse_timeout
        )
        for batch, bundle in bundles:
            # run_id 带批次后缀：每个 DAG 一个确定性 run_id，重复提交在 Airflow 侧幂等。
            run_id = f"ontometa__{artifact_id or 'manual'}__{batch['suffix']}"
            error: str | None = None
            triggered: dict[str, Any] = {}
            if bundle.dag_id not in parsed:
                # 落盘了但 Airflow 没解析到：多半是 dags 目录两侧不一致（失败模式 #3）。
                error = (
                    "Airflow 尚未解析到 DAG。产物经 SSH 投递到 Airflow 主机的 dags 目录"
                    "（设置页 → Airflow），请确认：①投递的 dags_dir 与 Airflow 的 "
                    "dags_folder 是同一目录；②DAG 源码无 import 错误（看 Airflow UI 的 "
                    "import errors）；③dag_dir_list_interval 较长的首次解析延迟。"
                )
            else:
                try:
                    client.unpause_dag(bundle.dag_id)
                    triggered = client.trigger_dag(bundle.dag_id, dag_run_id=run_id)
                except AirflowError as exc:
                    error = str(exc)
            batch_results.append(
                {
                    "suffix": batch["suffix"],
                    "dag_id": bundle.dag_id,
                    "dag_run_id": run_id,
                    "state": triggered.get("state")
                    or ("failed" if error else "queued"),
                    "run_url": client.run_url(bundle.dag_id, run_id),
                    "schedule": batch["schedule"],
                    # 该 DAG 实际会建的表（含只建表、无搬运作业的那些），不只是有作业的。
                    "tables": sorted(batch["ddl"]),
                    "jobs": [job.name for job in batch["jobs"]],
                    "error": error,
                    "artifacts": written_all.get(bundle.dag_id),
                }
            )
    finally:
        client.close()

    # 顶层字段向后兼容旧回执/前端：指向首批；权威列表是 ``batches``。
    first = batch_results[0] if batch_results else {}
    first_error = next((b["error"] for b in batch_results if b["error"]), None)
    return {
        "ontology_id": ontology_id,
        # 交 Airflow 编排执行。**这个字符串是对账的开关**：agent_pipeline
        # ``_reconcile_orchestrated_status`` 只认 "orchestrated"，而此前这里产的是
        # "flink_on_yarn"（全仓没有一处产 "orchestrated"），于是物化/同步制品从提交那刻起
        # 就是 SUCCEEDED、从不与真实 DagRun 对账。具体搬运通道另见 sync_tool。
        "execute_mode": "orchestrated",
        # 本次产出什么：ddl = 只建结构（物化），dml = 只搬数据（同步）。
        "emit": emit,
        # 搬运一律走 Flink SQL on YARN（统一执行架构），不再有多通道选择。
        "sync_tool": _SYNC_TOOL,
        # 这次搬运**真正生效**的 Flink 提交参数（设置页默认 + 本任务覆盖后的结果）。
        # 参数已逐任务不同，回执不写清就只能去翻 DAG 源码反推。建表不经 Flink，故只在
        # emit="dml" 时给。
        "flink": (
            flink_params.effective(flink_cfg, flink_task_params) if emit == "dml" else None
        ),
        "target_datasource": {"id": ds.id, "name": ds.name, "kind": ds.kind},
        "engine": engine,
        "database_prefix": database_prefix,
        "database_overrides": dict(database_overrides or {}),
        "table_overrides": dict(table_overrides or {}),
        "dag_id": first.get("dag_id"),
        "dag_run_id": first.get("dag_run_id"),
        "state": first.get("state"),
        "run_url": first.get("run_url"),
        "artifacts": first.get("artifacts"),
        "schedule": first.get("schedule"),
        # M16：一次编排可产多个 DAG（按 cron 分组 + 分批），逐个的触发结果在此。
        "batches": batch_results,
        # 本次会**建**的表。同步侧恒为空：前端（MaterializationContractPanel）拿历史回执的
        # tables 做字符串匹配来判「这张表已物化」，同步若填了表名就会让一次搬运冒充成物化。
        "tables": [q for q, _ in ddl_items],
        "jobs": [job.name for job in plan.jobs],
        "unsupported": plan.unsupported,
        "schema_notes": plan.schema_notes,
        "error": first_error,
        # 提交成功即 ok；真正的成败要看各 DagRun 状态，由前端轮询 status 端点。
        "ok": first_error is None,
    }


def _no_movable_objects_message(plan: JobPlan) -> str:
    """同步一个作业都编不出来时的错误文案：说清是哪些对象、为什么。

    最常见的成因是选中的对象都没有物理源表（人工建模的对象只有元数据），此时用户要做的
    是物化而不是同步——照抄 planner 给出的逐条 reason 比笼统说「无可搬对象」有用。
    """
    reasons = [
        f"{item.get('target') or '?'}（{item.get('reason') or '原因未给出'}）"
        for item in (plan.unsupported or [])[:10]
    ]
    tail = "" if len(plan.unsupported or []) <= 10 else f" 等 {len(plan.unsupported)} 项"
    detail = ("：" + "；".join(reasons) + tail) if reasons else "。"
    return (
        "没有任何对象可以同步，本次不会搬运任何数据" + detail + " "
        "人工建模的对象没有物理源表，只能物化建表，不能同步。"
    )


def _handoff_receipt(
    ontology_id: str,
    ds: DataSource,
    engine: str,
    emit: Emit,
    plan: JobPlan,
    ddl_items: list[tuple[str, str]],
    database_prefix: str | None,
    database_overrides: dict[str, str] | None,
    table_overrides: dict[str, str] | None,
) -> dict[str, Any]:
    """未配 Flink SqlRunner JAR 时的「仅产出」回执：不投递、不触发，只报会做什么。

    与 flink_job_runner 的 handoff 同义——数据搬运一律走 Flink，缺 JAR 就执行不了，
    如实说明而不是假装成功（见记忆 receipt-failure-vs-artifact-status）。

    **只有同步会走到这里**：建表是 ``SQLExecuteQueryOperator`` 直连目标仓，与 Flink 无关。
    """
    return {
        "ontology_id": ontology_id,
        "execute_mode": "handoff",
        "emit": emit,
        "sync_tool": _SYNC_TOOL,
        "note": (
            "未配置 Flink SqlRunner JAR（FLINK_SQL_RUNNER_JAR），ontoMeta 只产出搬运计划，"
            "不执行；配上 JAR 后重跑即提交到 YARN。"
        ),
        "target_datasource": {"id": ds.id, "name": ds.name, "kind": ds.kind},
        "engine": engine,
        "database_prefix": database_prefix,
        "database_overrides": dict(database_overrides or {}),
        "table_overrides": dict(table_overrides or {}),
        "tables": [q for q, _ in ddl_items],
        "jobs": [job.name for job in plan.jobs],
        "unsupported": plan.unsupported,
        "schema_notes": plan.schema_notes,
        "error": None,
        "ok": True,
    }


def _orchestrate(
    db: Session,
    ontology_id: str,
    *,
    emit: Emit,
    target_datasource_id: str,
    engine: str,
    database_prefix: str | None,
    database_overrides: dict[str, str] | None,
    table_overrides: dict[str, str] | None,
    selected_targets: list[str] | None,
    overrides: dict[str, dict[str, Any]] | None,
    refresh_cron: str | None,
    load_strategy: str | None,
    sync_contracts: bool,
    artifact_id: str | None,
    flink_task_params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """物化与同步共用的编排骨架：前置校验 → 契约对齐 → 生成 DDL → 交 ``_run_orchestrated``。

    **总是交 Airflow 编排执行**，不在本进程里落库。未配可用 Airflow 时直接报错——不再有
    直连落库（direct）回退：直连的 INSERT…SELECT 要求源表在目标数仓里可见，真实拓扑下
    不成立（见 `MATERIALIZE_ORCHESTRATION.md` §1）。

    ``emit="dml"`` 时 DDL 仍会生成但**不下发**（``ddl_items`` 置空后再进编排）：契约对齐与
    生成是判定「本次选中的对象要落到哪张表」的同一套推导，同步的搬运目标就来自它。
    """
    ds = db.get(DataSource, target_datasource_id)
    if ds is None:
        raise MaterializationError("目标数据源不存在")
    if not ds.dsn_secret_ref:
        raise MaterializationError(
            f"目标数据源「{ds.name}」未配置连接串（dsn），无法执行"
        )

    airflow = _settings.get_airflow_runtime(db)
    if not airflow.available:
        raise MaterializationError(
            "未配置可用的 Airflow（需在设置页填 endpoint 并启用），无法执行"
        )

    # 任务级 Flink 参数先校验：非法值要在动契约/生成 DDL 之前就说清楚，而不是等编排到
    # 一半从底层抛出一个 ValueError。
    try:
        flink_task_params = flink_params.normalize(flink_task_params)
    except flink_params.FlinkParamError as exc:
        raise MaterializationError(f"任务的 Flink 执行参数非法：{exc}") from exc

    # 契约是生成器的输入事实源：先对齐，保证 materialized/层/分区等为最新，
    # 再应用人工覆盖（override 会钉住，后续机器推导不覆盖）。
    if sync_contracts:
        _contract_service.sync(db, ontology_id)
    patches: dict[str, dict[str, Any]] = {k: dict(v) for k, v in (overrides or {}).items()}
    if refresh_cron is not None:
        for contract in _contract_service.list_selected(db, ontology_id, selected_targets):
            patches.setdefault(contract.id, {}).setdefault("refresh_cron", refresh_cron)
    for contract_id, patch in patches.items():
        _contract_service.update(db, contract_id, patch)

    ddl = _generator.generate_ddl(
        db,
        ontology_id,
        engine,
        database_prefix=database_prefix,
        database_overrides=database_overrides,
        table_overrides=table_overrides,
    )

    selected = _selected_names(db, ontology_id, selected_targets, table_overrides)
    if emit == "ddl":
        ddl_items = _select(ddl["statements"], selected)
        constraint_items = _select_constraints(ddl.get("constraints"), ddl_items)
    else:
        # 同步只搬数据：一条建表语句都不下发。目标表必须已由物化建好——除了「物化在先」
        # 这条策略，staging 的 CREATE TABLE … LIKE <正式表> 结构上也要求它先存在。
        ddl_items, constraint_items = [], {}

    return _run_orchestrated(
        db,
        ontology_id,
        emit=emit,
        ds=ds,
        engine=engine,
        airflow=airflow,
        ddl_items=ddl_items,
        constraint_items=constraint_items,
        database_prefix=database_prefix,
        database_overrides=database_overrides,
        table_overrides=table_overrides,
        selected_targets=selected_targets,
        artifact_id=artifact_id,
        load_strategy=load_strategy,
        flink_task_params=flink_task_params,
    )


def run_materialize(
    db: Session,
    ontology_id: str,
    *,
    target_datasource_id: str,
    engine: str,
    database_prefix: str | None = None,
    database_overrides: dict[str, str] | None = None,
    table_overrides: dict[str, str] | None = None,
    selected_targets: list[str] | None = None,
    overrides: dict[str, dict[str, Any]] | None = None,
    refresh_cron: str | None = None,
    sync_contracts: bool = True,
    artifact_id: str | None = None,
    flink_task_params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """**物化 = 建结构**：把本体想要的表在目标数据源里建出来，返回回执 dict。

    产出的 DAG 只有 ``read_spec → create_tables [→ add_constraints]``，``schedule=None``
    （建表是幂等的一次性动作，挂到定时 DAG 上只会每轮重复跑一遍 CREATE）。
    **不产任何搬运作业、不产 staging/swap，一行数据都不动**——要把数据搬进来是同步
    （``run_sync``）的事。故本函数没有 ``load_strategy``：装载方式只对搬运有意义。

    幂等：各引擎的建表语句一律 ``CREATE TABLE IF NOT EXISTS``，重复物化跳过已存在的表，
    既有数据原封不动（``generate_ddl`` 从不产 DROP/TRUNCATE，也从不 ALTER）。

    零张表是空操作（回执 ``tables`` 为空、不报错）——与同步「零个可搬对象必须大声拒绝」
    不同：没有要建的表说明本体里的表都已被别的制品覆盖，不是故障。

    ``overrides``：``{contract_id: {字段: 值}}``，弹窗里人工改的存储策略/层/表名等。
    经 ``MaterializationContractService.update`` 写回并钉住，使生成读到的契约与展示
    一致（不另存一份配置）。

    ``refresh_cron``：**整批**调度，展开到本次选中的每个契约。它不影响本次产出的建表
    DAG（那条恒为 schedule=None），只写回契约供**后续同步**分组用。弹窗逐实体配 cron，
    而 Data Agent 那边「每天凌晨跑一次」说的是整批，不该要求调用方先知道契约 id。
    展开在契约对齐**之后**做，故新推导出来的契约也能被覆盖到；``overrides`` 里显式给了
    refresh_cron 的实体保持不变——细粒度优先于整批默认。

    ``database_overrides``（层 → 库名）与 ``table_overrides``（contract_id → 表名）
    是本次落库的目标位置，只作用于本次生成、不写回契约。

    ``flink_task_params``：**这个任务自己的** Flink 提交参数（见
    :mod:`app.services.flink_params`），留空的项跟随设置页默认值。物化本身不经 Flink
    （建表走 SQLExecuteQueryOperator），收下它只为签名与同步一致、Spec 可原样透传。
    """
    return _orchestrate(
        db,
        ontology_id,
        emit="ddl",
        target_datasource_id=target_datasource_id,
        engine=engine,
        database_prefix=database_prefix,
        database_overrides=database_overrides,
        table_overrides=table_overrides,
        selected_targets=selected_targets,
        overrides=overrides,
        refresh_cron=refresh_cron,
        load_strategy=None,
        sync_contracts=sync_contracts,
        artifact_id=artifact_id,
        flink_task_params=flink_task_params,
    )


def run_sync(
    db: Session,
    ontology_id: str,
    *,
    target_datasource_id: str,
    engine: str,
    database_prefix: str | None = None,
    database_overrides: dict[str, str] | None = None,
    table_overrides: dict[str, str] | None = None,
    load_strategy: str | None = None,
    selected_targets: list[str] | None = None,
    overrides: dict[str, dict[str, Any]] | None = None,
    refresh_cron: str | None = None,
    sync_contracts: bool = True,
    artifact_id: str | None = None,
    flink_task_params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """**同步 = 搬数据**：把源表的数据搬进目标表，返回回执 dict。

    只产 Flink SQL 搬运作业（全量走 staging + 原子切换），**不建业务表**：回执的 ``tables``
    恒为空。目标表须已由 ``run_materialize`` 建好——除了「物化在先」这条策略，staging 的
    ``CREATE TABLE … LIKE <正式表>`` 结构上也要求它先存在。

    一个作业都编不出来时**抛 ``MaterializationError``**，不回 ``ok: True``：那意味着没有
    任何数据会被搬，最常见的成因是选中的对象都是人工建模的（没有物理源表，只能物化）。

    ``load_strategy``：**本次运行的全局覆盖**（Spec 里选的「全量/增量」），缺省 None
    = 逐表按契约。它此前只是个签名上的摆设——收下就丢，于是 Spec 上写着 full、
    DAG 里跑的却是契约的 incremental，连带 M15 的 staging+切换（只在全量时挂）
    从来没被触发过。要改某张表的常态策略仍走 ``overrides`` 写回契约，两者不冲突。

    ``refresh_cron``：**整批**调度，展开到本次选中的每个契约，再由 ``_cron_by_entity``
    决定各 DAG 的 schedule（一个 cron 一个 DAG）。语义与 ``run_materialize`` 的同名形参
    一致，区别是这里真的会作用到本次产出的 DAG 上。

    ``flink_task_params``：这条同步自己的 Flink 提交参数（并行度/队列/提交目标/
    checkpoint/额外 -D）。**搬运真的经 Flink**，故它在这里逐字生效：填了的项覆盖设置页
    默认，留空的项跟随设置页。CDC/增量的 checkpoint 目录也从这里取（缺则用设置页那份，
    再缺就在编译期报错——读位点无处持久化）。

    其余形参见 ``run_materialize``。
    """
    return _orchestrate(
        db,
        ontology_id,
        emit="dml",
        target_datasource_id=target_datasource_id,
        engine=engine,
        database_prefix=database_prefix,
        database_overrides=database_overrides,
        table_overrides=table_overrides,
        selected_targets=selected_targets,
        overrides=overrides,
        refresh_cron=refresh_cron,
        load_strategy=load_strategy,
        sync_contracts=sync_contracts,
        artifact_id=artifact_id,
        flink_task_params=flink_task_params,
    )
