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

from typing import Any

from sqlalchemy.orm import Session

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


def _schedule_of(db: Session, ontology_id: str, selected: set[str] | None) -> str | None:
    """本次物化的调度表达式：取选中实体契约里出现最多的 refresh_cron。

    契约的定时策略是逐实体的，而一个 DAG 只能有一个 schedule。取众数而非报错——
    多数场景下同一批表用同一个节奏；真有分歧的，DAG 里各表仍是同一批跑，
    差异体现在人怎么分批提交，而不是让这里编一个折中值。
    """
    from collections import Counter

    contracts = _contract_service.list_contracts(db, ontology_id, materialized_only=True)
    if selected:
        names = _contract_service.resolve_target_names(db, contracts)
        contracts = [
            c for c in contracts if (names.get(c.target_id) or (None,))[0] in selected
        ]
    crons = [c.refresh_cron for c in contracts if (c.refresh_cron or "").strip()]
    if not crons:
        return None
    return Counter(crons).most_common(1)[0][0]


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
    bundle = _dag_builder.build(
        ontology_id=ontology_id,
        plan=plan,
        ddl_statements=dict(ddl_items),
        schedule=_schedule_of(db, ontology_id, set(selected_targets or []) or None),
        channel=channel,
        runner_endpoint=airflow.sync_runner_endpoint,
        tool=sync_tool,
        engine=engine,
        warehouse_conn_id=_warehouse_conn_id(ds),
        # 以下均为 docker 通道字段，runner 通道忽略（build 内部按 channel 分流）。
        docker_network=airflow.docker_network,
        jobs_host_dir=airflow.jobs_dir,
        drivers_host_dir=airflow.drivers_dir,
        image_overrides=airflow.sync_tool_images,
        # M11：DAG 任务的 outlets 注入真实目标表 URN，供 DataHub Airflow 插件自动上报
        # 血缘。与兜底 emitter 走同一个 build_dataset_urn，两条路径产同一份 URN。
        target_urn_builder=lambda job: build_dataset_urn(
            job.target.platform, job.target.qualified, fabric
        ),
    )
    try:
        written = bundle.write(airflow.dags_dir, airflow.jobs_dir)
    except OSError as exc:
        raise MaterializationError(f"DAG 投递失败（{airflow.dags_dir}）：{exc}") from exc

    # run_id 取制品 id：Airflow 对重复 run_id 返回 409，重复提交因而天然幂等。
    run_id = f"ontometa__{artifact_id or 'manual'}"
    client = AirflowClient(
        airflow.endpoint,
        username=airflow.username,
        password=airflow.password,
        token=airflow.token,
        api_version=airflow.api_version,
    )
    triggered: dict[str, Any] = {}
    error: str | None = None
    try:
        client.unpause_dag(bundle.dag_id)
        triggered = client.trigger_dag(bundle.dag_id, dag_run_id=run_id)
    except AirflowError as exc:
        # 不抛：DAG 与作业配置**已经落盘**，回执要如实反映「产物已出、触发失败」，
        # 而不是让人以为整件事没发生。DAG 需要 Airflow 解析后才可触发，首次提交
        # 常见于「解析尚未完成」，重试即可。
        error = str(exc)
    finally:
        client.close()

    state = triggered.get("state") or ("failed" if error else "queued")
    return {
        "ontology_id": ontology_id,
        "execute_mode": "orchestrated",
        "sync_channel": channel,
        "target_datasource": {"id": ds.id, "name": ds.name, "kind": ds.kind},
        "engine": engine,
        "database_prefix": database_prefix,
        "database_overrides": dict(database_overrides or {}),
        "table_overrides": dict(table_overrides or {}),
        "dag_id": bundle.dag_id,
        "dag_run_id": run_id,
        "state": state,
        "run_url": client.run_url(bundle.dag_id, run_id),
        "artifacts": written,
        "schedule": bundle.spec.get("schedule"),
        "tables": [q for q, _ in ddl_items],
        "jobs": [job.name for job in plan.jobs],
        "unsupported": plan.unsupported,
        "schema_notes": plan.schema_notes,
        "error": error,
        # 提交成功即 ok；真正的成败要看 DagRun 状态，由前端轮询 status 端点。
        "ok": error is None,
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
