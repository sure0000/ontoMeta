"""本体一键物化编排：生成建表 DDL + 搬运作业 + DAG → 投递给 Airflow 执行 → 回执。

本模块把已有能力串成一次可执行的物化，不重造任何一块：

- **物化契约**（``services/materialization_contract``）：先 ``sync`` 保证契约存在/最新，
  弹窗覆盖的存储策略/表名作为 override 写回并钉住，使“生成”与“展示”同一事实源。
- **正向生成器**（``services/warehouse_generator``）：按目标引擎产出建表 DDL（本体反补的
  注释/分区/主键声明只在这条路径上）。
- **搬运作业编译**（``services/job_planner`` + ``app/warehouse/jobs``）：本体+契约 → JobSpec
  → 选定工具（seatunnel/datax/flink）的作业配置。
- **DAG 生成与投递**（``services/airflow_dag_builder`` + ``connectors/airflow``）：产物落盘交
  Airflow，触发一次 DagRun。落库由 Airflow 按 conn_id 执行，ontoMeta 不直连目标库。
"""

from __future__ import annotations

import hashlib
import time
from typing import Any

from sqlalchemy.orm import Session

from app.config import settings as env_settings

from app.connectors.airflow import AirflowClient, AirflowError
from app.connectors.datahub import build_dataset_urn
from app.connectors.sync_runner import (
    EXPECTED_CONTRACT_VERSION,
    SyncRunnerClient,
    SyncRunnerError,
)
from app.models.data_app import DataSource
from app.services.airflow_dag_builder import AirflowDagBuilder
from app.services.job_planner import JobPlanner
from app.services.materialization_contract import MaterializationContractService
from app.services.settings_service import SettingsService
from app.services.warehouse_generator import WarehouseGenerator
from app.warehouse.jobs import (
    JobPlan,
    SyncImageUnavailableError,
    get_job_adapter,
    resolve_docker_image,
)

_contract_service = MaterializationContractService()
_generator = WarehouseGenerator()
_job_planner = JobPlanner()
_dag_builder = AirflowDagBuilder()
_settings = SettingsService()


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


def _wait_for_parse(client: AirflowClient, dag_id: str, timeout: float) -> bool:
    """轮询 ``GET /dags/{id}`` 直到 Airflow 解析到该 DAG，或超时。

    替代「落盘后立刻触发、404 被吞成回执 error」：首次提交常见于解析尚未完成，
    直接触发必 404。⚠ Airflow ``dag_dir_list_interval`` 默认 300s（§8.1），超时值
    由 ``ONTOMETA_DAG_PARSE_TIMEOUT`` 配；超时不抛，交由调用方记「尚未解析到」。
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            if client.dag_exists(dag_id):
                return True
        except AirflowError:
            pass  # 鉴权/网络问题由触发那步的错误体带出，这里只管「在不在」
        if time.monotonic() >= deadline:
            return False
        time.sleep(2)


def _run_orchestrated(
    db: Session,
    ontology_id: str,
    *,
    ds: DataSource,
    engine: str,
    sync_tool: str | None,
    airflow,
    ddl_items: list[tuple[str, str]],
    database_prefix: str | None,
    database_overrides: dict[str, str] | None,
    table_overrides: dict[str, str] | None,
    selected_targets: list[str] | None,
    artifact_id: str | None,
) -> dict[str, Any]:
    """产出 DAG 与搬运作业 → 投递 → 触发一次运行。**不在本进程里落库**。"""
    # 执行通道（M14）：runner 走常驻服务，docker 走旧的兄弟容器通道。
    channel = (airflow.sync_channel or "runner").lower()
    runner_caps: dict | None = None
    if channel == "runner":
        # runner 通道的前置条件在提交前问清楚，别产出一个连不上 runner 的 DAG。
        if not airflow.sync_runner_endpoint:
            raise MaterializationError(
                "sync_channel=runner 但未配置 sync-runner 地址"
                "（设 ONTOMETA_SYNC_RUNNER_ENDPOINT），无法物化"
            )
        client = SyncRunnerClient(airflow.sync_runner_endpoint)
        try:
            runner_caps = client.capabilities()
        except SyncRunnerError as exc:
            raise MaterializationError(
                f"sync-runner 不可达（{airflow.sync_runner_endpoint}）：{exc}"
            ) from exc
        finally:
            client.close()
        # 契约版本不匹配即拒绝，而不是发过去再看会不会炸（§3.5）。
        got = runner_caps.get("contract_version")
        if got != EXPECTED_CONTRACT_VERSION:
            raise MaterializationError(
                f"sync-runner 契约版本不匹配（runner={got}，"
                f"ontoMeta 认识 {EXPECTED_CONTRACT_VERSION}），请升级对应一侧"
            )
    else:
        # docker 通道：镜像可用性先于一切，拿不到镜像就不生成 DAG、不触发。
        try:
            resolve_docker_image(get_job_adapter(sync_tool), airflow.sync_tool_images)
        except SyncImageUnavailableError as exc:
            raise MaterializationError(str(exc)) from exc

    plan = _job_planner.build(
        db,
        ontology_id,
        engine=engine,
        tool=sync_tool,
        target_alias=_warehouse_conn_id(ds),
        database_prefix=database_prefix,
        database_overrides=database_overrides,
        table_overrides=table_overrides,
        selected_targets=selected_targets,
        # runner 通道按执行侧 capabilities 判可搬性，替代硬编码的工具平台表。
        runner_capabilities=runner_caps,
    )
    # 目标表 URN 的 fabric 取自 DataHub 设置页（默认 PROD），与兜底 emitter 同一来源。
    fabric = _settings.get_datahub_runtime(db).fabric

    def _urn_builder(job):
        return build_dataset_urn(job.target.platform, job.target.qualified, fabric)

    # 按 cron 分组 + 按上限分批：一个 cron 一个 DAG、少数派不再被众数吞掉，超上限再拆（M16）。
    cron_map = _cron_by_entity(db, ontology_id)
    batches = _plan_batches(
        plan.jobs, cron_map, env_settings.ontometa_max_tasks_per_dag
    )
    # 无可搬作业但有建表：仍产一个 create_tables-only 的 manual DAG（保持既有「只建表」行为）。
    if not batches and ddl_items:
        batches = [{"suffix": "manual", "schedule": None, "jobs": ()}]

    bundles: list[tuple[dict, Any]] = []
    for batch in batches:
        targets = {job.target.qualified for job in batch["jobs"]}
        ddl_subset = (
            {q: s for q, s in ddl_items if q in targets}
            if batch["jobs"]
            else dict(ddl_items)  # 只建表的兜底批：全部 DDL
        )
        bundle = _dag_builder.build(
            ontology_id=ontology_id,
            plan=JobPlan(jobs=batch["jobs"]),
            ddl_statements=ddl_subset,
            schedule=batch["schedule"],
            channel=channel,
            runner_endpoint=airflow.sync_runner_endpoint,
            dag_id_suffix=batch["suffix"],
            max_active_tasks=env_settings.ontometa_max_active_tasks_per_dag,
            tool=sync_tool,
            engine=engine,
            warehouse_conn_id=_warehouse_conn_id(ds),
            docker_network=airflow.docker_network,
            jobs_host_dir=airflow.jobs_dir,
            drivers_host_dir=airflow.drivers_dir,
            image_overrides=airflow.sync_tool_images,
            target_urn_builder=_urn_builder,
        )
        bundles.append((batch, bundle))

    # 先全部落盘：等解析时一次 dag_dir 扫描即可全部认到，避免逐个各等一个解析周期。
    written_all: dict[str, dict] = {}
    for _, bundle in bundles:
        try:
            written_all[bundle.dag_id] = bundle.write(airflow.dags_dir, airflow.jobs_dir)
        except OSError as exc:
            raise MaterializationError(
                f"DAG 投递失败（{airflow.dags_dir}）：{exc}"
            ) from exc

    client = AirflowClient(
        airflow.endpoint,
        username=airflow.username,
        password=airflow.password,
        token=airflow.token,
        api_version=airflow.api_version,
    )
    batch_results: list[dict] = []
    parse_timeout = env_settings.ontometa_dag_parse_timeout
    try:
        for batch, bundle in bundles:
            # run_id 带批次后缀：每个 DAG 一个确定性 run_id，重复提交在 Airflow 侧幂等。
            run_id = f"ontometa__{artifact_id or 'manual'}__{batch['suffix']}"
            error: str | None = None
            triggered: dict[str, Any] = {}
            if not _wait_for_parse(client, bundle.dag_id, parse_timeout):
                # 落盘了但 Airflow 没解析到：多半是 dags 目录两侧不一致（失败模式 #3）。
                error = (
                    "Airflow 尚未解析到 DAG，请检查 dags 目录是否双向可见"
                    "（ontoMeta 写的与 Airflow 读的是否同一路径）"
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
                    "tables": sorted({job.target.qualified for job in batch["jobs"]}),
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
        "execute_mode": "orchestrated",
        "sync_channel": channel,
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
        # M16：一次物化可产多个 DAG（按 cron 分组 + 分批），逐个的触发结果在此。
        "batches": batch_results,
        "tables": [q for q, _ in ddl_items],
        "jobs": [job.name for job in plan.jobs],
        "unsupported": plan.unsupported,
        "schema_notes": plan.schema_notes,
        "error": first_error,
        # 提交成功即 ok；真正的成败要看各 DagRun 状态，由前端轮询 status 端点。
        "ok": first_error is None,
    }


def run(
    db: Session,
    ontology_id: str,
    *,
    target_datasource_id: str,
    engine: str,
    sync_tool: str | None = None,
    database_prefix: str | None = None,
    database_overrides: dict[str, str] | None = None,
    table_overrides: dict[str, str] | None = None,
    load_strategy: str | None = None,
    selected_targets: list[str] | None = None,
    overrides: dict[str, dict[str, Any]] | None = None,
    sync_contracts: bool = True,
    artifact_id: str | None = None,
) -> dict[str, Any]:
    """物化一个本体到目标数据源，返回回执 dict。

    **总是交 Airflow 编排执行**：产出建表 DDL + 搬运作业 + DAG，投递给 Airflow 并
    触发一次运行，**不在本进程里落库**。未配可用 Airflow 时直接报错——不再有
    直连落库（direct）回退：直连的 INSERT…SELECT 要求源表在目标数仓里可见，
    真实拓扑下不成立（见 `MATERIALIZE_ORCHESTRATION.md` §1）。

    搬运工具（``sync_tool``）与同步策略由物化弹窗逐次选；同步策略随 ``overrides``
    写回契约，``JobPlanner`` 据契约逐表决定装载方式。

    ``overrides``：``{contract_id: {字段: 值}}``，弹窗里人工改的存储策略/层/表名等。
    经 ``MaterializationContractService.update`` 写回并钉住，使生成读到的契约与展示
    一致（不另存一份配置）。

    ``database_overrides``（层 → 库名）与 ``table_overrides``（contract_id → 表名）
    是本次落库的目标位置，只作用于本次生成、不写回契约。
    """
    ds = db.get(DataSource, target_datasource_id)
    if ds is None:
        raise MaterializationError("目标数据源不存在")
    if not ds.dsn_secret_ref:
        raise MaterializationError(
            f"目标数据源「{ds.name}」未配置连接串（dsn），无法物化"
        )

    airflow = _settings.get_airflow_runtime(db)
    if not airflow.available:
        raise MaterializationError(
            "未配置可用的 Airflow（需在设置页填 endpoint 并启用），无法执行物化"
        )

    # 契约是生成器的输入事实源：先对齐，保证 materialized/层/分区等为最新，
    # 再应用人工覆盖（override 会钉住，后续机器推导不覆盖）。
    if sync_contracts:
        _contract_service.sync(db, ontology_id)
    for contract_id, patch in (overrides or {}).items():
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
    ddl_items = _select(ddl["statements"], selected)

    return _run_orchestrated(
        db,
        ontology_id,
        ds=ds,
        engine=engine,
        sync_tool=sync_tool,
        airflow=airflow,
        ddl_items=ddl_items,
        database_prefix=database_prefix,
        database_overrides=database_overrides,
        table_overrides=table_overrides,
        selected_targets=selected_targets,
        artifact_id=artifact_id,
    )
