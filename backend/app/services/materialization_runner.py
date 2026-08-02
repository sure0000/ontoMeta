"""本体一键物化编排：生成 DDL/ETL → 对目标数据源真正落库 → 回执。

本模块把已有的三块能力串成一次可执行的物化，不重造任何一块：

- **物化契约**（``services/materialization_contract``）：先 ``sync`` 保证契约存在/最新，
  弹窗覆盖的存储策略/表名作为 override 写回并钉住，使"生成"与"展示"同一事实源。
- **正向生成器**（``services/warehouse_generator``）：按目标引擎产出建表 DDL 与
  ODS→目标层的 ETL SQL；已按契约 ``materialized`` 过滤，本模块只再按用户勾选裁剪。
- **写侧执行器**（``services/data_app_executor.execute_write``）：把语句真正打到目标
  ``DataSource`` 的 DSN 上，单事务、失败回滚。

方言现实：生成的 DDL 是数仓方言（Hive/Doris/…），目标 DataSource 的 DSN 必须是对应
数仓引擎；本地 SQLite/DuckDB 无对应 adapter，不能承载完整 DDL（见 test 与文档）。
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from app.connectors.airflow import AirflowClient, AirflowError
from app.models.data_app import DataSource
from app.services import data_app_executor
from app.services.airflow_dag_builder import AirflowDagBuilder
from app.services.job_planner import JobPlanner
from app.services.materialization_contract import MaterializationContractService
from app.services.settings_service import SettingsService
from app.services.warehouse_generator import WarehouseGenerator

_contract_service = MaterializationContractService()
_generator = WarehouseGenerator()
_job_planner = JobPlanner()
_dag_builder = AirflowDagBuilder()
_settings = SettingsService()


class MaterializationError(ValueError):
    """物化前置条件错误（目标源不存在 / 未配置连接串等），面向用户可读。"""


def _loads(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None


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


def _run_phase(
    dsn: str, items: list[tuple[str, str]], mapping: dict[str, Any] | None
) -> dict[str, Any]:
    """执行一批语句并把回执的 per_statement 归位到 qualified name。"""
    receipt = data_app_executor.execute_write(
        dsn=dsn, statements=[sql for _, sql in items], mapping=mapping
    )
    for ps in receipt.get("per_statement", []):
        idx = ps.get("index")
        if isinstance(idx, int) and 0 <= idx < len(items):
            ps["target"] = items[idx][0]
    receipt["targets"] = [q for q, _ in items]
    return receipt


def _skipped_phase(total: int, reason: str) -> dict[str, Any]:
    return {
        "total": total,
        "executed": 0,
        "failed": 0,
        "error": None,
        "skipped": True,
        "skip_reason": reason,
        "per_statement": [],
        "targets": [],
    }


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
    airflow,
    ddl_items: list[tuple[str, str]],
    database_prefix: str | None,
    database_overrides: dict[str, str] | None,
    table_overrides: dict[str, str] | None,
    selected_targets: list[str] | None,
    artifact_id: str | None,
) -> dict[str, Any]:
    """产出 DAG 与搬运作业 → 投递 → 触发一次运行。**不在本进程里落库**。"""
    plan = _job_planner.build(
        db,
        ontology_id,
        engine=engine,
        target_alias=airflow.warehouse_conn_id,
        database_prefix=database_prefix,
        database_overrides=database_overrides,
        table_overrides=table_overrides,
        selected_targets=selected_targets,
    )
    bundle = _dag_builder.build(
        ontology_id=ontology_id,
        plan=plan,
        ddl_statements=dict(ddl_items),
        schedule=_schedule_of(db, ontology_id, set(selected_targets or []) or None),
        engine=engine,
        warehouse_conn_id=airflow.warehouse_conn_id,
        seatunnel_image=airflow.seatunnel_image,
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
    database_prefix: str | None = None,
    database_overrides: dict[str, str] | None = None,
    table_overrides: dict[str, str] | None = None,
    load_strategy: str | None = None,
    selected_targets: list[str] | None = None,
    overrides: dict[str, dict[str, Any]] | None = None,
    sync_contracts: bool = True,
    execute_mode: str | None = None,
    artifact_id: str | None = None,
) -> dict[str, Any]:
    """物化一个本体到目标数据源，返回回执 dict。

    两种执行方式（``execute_mode``）：

    - ``orchestrated``（默认，配了 Airflow 时）：产出建表 DDL + 搬运作业 + DAG，投递给
      Airflow 并触发一次运行。**跨源搬运只有这条路走得通**——直连的 INSERT…SELECT 要求
      源表在目标数仓里可见，真实拓扑下不成立（见 `MATERIALIZE_ORCHESTRATION.md` §1）。
    - ``direct``：ontoMeta 直连目标库执行 DDL/ETL，即改造前的行为。**开发模式**，
      供没有 Airflow 的本地环境跑通链路；未配置 Airflow 时自动回落到它。

    direct 下先建表（DDL），全部成功后再落数（ETL）——表不存在时装载没有意义，故 DDL 有失败
    即跳过 ETL，回执里显式标注，绝不静默。

    ``overrides``：``{contract_id: {字段: 值}}``，弹窗里人工改的存储策略/层/表名等。
    经 ``MaterializationContractService.update`` 写回并钉住，使生成读到的契约与展示
    一致（不另存一份配置）。

    ``database_overrides``（层 → 库名）与 ``table_overrides``（contract_id → 表名）
    是本次落库的目标位置，只作用于本次生成、不写回契约——库/表名属于「落到哪」的运行
    期选择，与契约描述的「怎么建」是两回事。
    """
    ds = db.get(DataSource, target_datasource_id)
    if ds is None:
        raise MaterializationError("目标数据源不存在")
    if not ds.dsn_secret_ref:
        raise MaterializationError(
            f"目标数据源「{ds.name}」未配置连接串（dsn），无法落库"
        )

    # 契约是生成器的输入事实源：先对齐，保证 materialized/层/分区等为最新，
    # 再应用人工覆盖（override 会钉住，后续机器推导不覆盖）。
    if sync_contracts:
        _contract_service.sync(db, ontology_id)
    for contract_id, patch in (overrides or {}).items():
        _contract_service.update(db, contract_id, patch)

    # 同步方式为本次物化运行期的一次性选择：只作用于本次生成的 ETL，不写回契约
    # （避免默认全量把契约既定的增量策略钉死；契约策略仍由契约编辑/同步作业各自维护）。
    ddl = _generator.generate_ddl(
        db,
        ontology_id,
        engine,
        database_prefix=database_prefix,
        database_overrides=database_overrides,
        table_overrides=table_overrides,
    )
    etl = _generator.generate_etl_sql(
        db,
        ontology_id,
        engine,
        database_prefix=database_prefix,
        database_overrides=database_overrides,
        table_overrides=table_overrides,
        load_strategy=load_strategy,
        # 弹窗逐实体选同步方式（已随 overrides 写回契约），故按契约逐表生成；
        # 若调用方仍给了全局 load_strategy，它优先。
        per_contract_strategy=True,
    )

    selected = _selected_names(db, ontology_id, selected_targets, table_overrides)
    ddl_items = _select(ddl["statements"], selected)
    etl_items = _select(etl["statements"], selected)
    mapping = _loads(ds.mapping_json)

    airflow = _settings.get_airflow_runtime(db)
    mode = (execute_mode or "").strip().lower() or (
        "orchestrated" if airflow.available else "direct"
    )
    if mode == "orchestrated":
        if not airflow.available:
            raise MaterializationError(
                "未配置可用的 Airflow（需填 endpoint / DAG 目录 / 作业目录并启用），"
                "无法编排执行；如仅本地验证可改用 direct 开发模式"
            )
        return _run_orchestrated(
            db,
            ontology_id,
            ds=ds,
            engine=engine,
            airflow=airflow,
            ddl_items=ddl_items,
            database_prefix=database_prefix,
            database_overrides=database_overrides,
            table_overrides=table_overrides,
            selected_targets=selected_targets,
            artifact_id=artifact_id,
        )
    if mode != "direct":
        raise MaterializationError(f"未知执行方式 {mode!r}，可选：orchestrated / direct")

    ddl_receipt = _run_phase(ds.dsn_secret_ref, ddl_items, mapping)
    ddl_ok = ddl_receipt["failed"] == 0 and ddl_receipt["error"] is None
    if ddl_ok:
        etl_receipt = _run_phase(ds.dsn_secret_ref, etl_items, mapping)
    else:
        etl_receipt = _skipped_phase(len(etl_items), "建表未全部成功，跳过数据装载")

    ok = ddl_ok and etl_receipt["failed"] == 0 and etl_receipt.get("error") is None
    return {
        "ontology_id": ontology_id,
        "execute_mode": "direct",
        "target_datasource": {"id": ds.id, "name": ds.name, "kind": ds.kind},
        "engine": engine,
        "database_prefix": database_prefix,
        "database_overrides": dict(database_overrides or {}),
        "table_overrides": dict(table_overrides or {}),
        "tables": [q for q, _ in ddl_items],
        "ddl": ddl_receipt,
        "etl": etl_receipt,
        "warnings": (ddl.get("warnings") or []) + (etl.get("warnings") or []),
        "unsupported": (ddl.get("unsupported") or []) + (etl.get("unsupported") or []),
        "ok": ok,
    }
