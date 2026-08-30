"""数仓路由：物化契约（M1）与本体 → 物理正向生成（M3）。

本体是权威、物理表是投影；物化契约补齐本体不承载的落地配置
（目标层、引擎、增量策略、分区键、SCD、刷新频率），生成器据此产出各引擎产物。
"""

from dataclasses import asdict
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import (
    agent_pipeline,
    datahub_writeback,
    lineage_emitter,
    materialization_contract_service,
    settings_service,
    warehouse_generator,
)
from app.database import get_db
from app.models import MaterializationContract, Ontology
from app.models.agent import ArtifactStatus
from app.models.ontology import OntologyStatus
from app.schemas import (
    IngestionContractInput,
    IngestionContractOut,
    IngestionTaskResultInput,
    MaterializationContractOut,
    MaterializationContractSyncResult,
    MaterializationContractUpdate,
    MaterializePreflightRequest,
    MaterializePreflightResult,
    MaterializeRequest,
    MaterializeResult,
)
from app.warehouse import (
    DEFAULT_ENGINE,
    UnknownEngineError,
    get_adapter,
    list_adapters,
    list_engines,
)
from app.warehouse.adapters.base import UnimplementedAdapter
from app.warehouse.policy import WAREHOUSE_ENGINE, require_doris

router = APIRouter()


def _require_ontology(db: Session, ontology_id: str) -> Ontology:
    ontology = db.query(Ontology).filter(Ontology.id == ontology_id).first()
    if ontology is None:
        raise HTTPException(status_code=404, detail="本体不存在")
    return ontology


def _require_engine(engine: str) -> str:
    """Validate an engine for historical read/generation endpoints.

    New write work uses ``_require_new_warehouse_engine``; keeping this helper
    compatible lets operators inspect old Hive/StarRocks artifacts during the
    migration window without making them executable as new Doris work.
    """
    try:
        get_adapter(engine)
    except UnknownEngineError:
        raise HTTPException(
            status_code=400,
            detail=f"未知引擎 {engine}，可选：{', '.join(list_engines())}",
        ) from None
    return engine


def _require_new_warehouse_engine(engine: str) -> str:
    try:
        return require_doris(engine, operation="新建数仓任务")
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"数仓新任务只允许 Doris；收到引擎 {engine!r}",
        ) from None


def _to_out(
    contract: MaterializationContract,
    names: dict[str, tuple[str | None, str | None]],
) -> MaterializationContractOut:
    out = MaterializationContractOut.model_validate(contract)
    name, display_name = names.get(contract.target_id, (None, None))
    out.target_name = name
    out.target_display_name = display_name
    return out


def _auto_table_name(layer: str, name: str) -> str:
    """自动表名：层_实体名（已带层前缀则不重复加）。与前端 recommendTableName 同口径。"""
    return name if name.startswith(f"{layer}_") else f"{layer}_{name}"


@router.get("/materialize/targets")
def list_materialize_targets(db: Session = Depends(get_db)):
    """物化任务选择用：每个数据域一个**工作本体**及其**可物化实体**（业务对象 +
    事实/桥表关系），附自动生成的表名。一次请求拿全树，供前端级联选择 + 搜索。

    **为什么在后端聚合**：
    - 去重：每域只取一个工作本体（优先最新已发布，否则最新更新），避免多版本/草稿同名实体刷屏。
    - 只出可物化实体：直接用契约 ``derive``（实时推导，不读可能过期的已存契约），外键/
      派生等**不落表**的关系天然被排除——外键无承接表，无法同步，不应可选。
    - 同本体内按实体名去重：可物化实体名应唯一，重名是命名问题，不在选择面重复呈现。
    """
    from sqlalchemy import func as _func

    from app.models import DomainContext, ObjectType, RelationType

    # 每域的工作本体：先取最新已发布，没有则取最新更新的一个。
    ontologies = (
        db.query(Ontology).order_by(Ontology.updated_at.desc()).all()
    )
    working: dict[str, Ontology] = {}
    for o in ontologies:
        cur = working.get(o.domain_context_id)
        if cur is None:
            working[o.domain_context_id] = o
        elif (
            o.status == OntologyStatus.PUBLISHED.value
            and cur.status != OntologyStatus.PUBLISHED.value
        ):
            working[o.domain_context_id] = o

    domain_names = {
        d.id: d.name for d in db.query(DomainContext).all()
    }

    result: list[dict] = []
    for domain_id, ont in working.items():
        derived = materialization_contract_service.derive(db, ont.id)
        rows = [d for d in derived if d.get("materialized")]
        if not rows:
            continue
        # 实体名/显示名（避免 N+1）。
        obj_ids = [
            r["target_id"] for r in rows if r["target_kind"] == "object_type"
        ]
        rel_ids = [
            r["target_id"] for r in rows if r["target_kind"] == "relation_type"
        ]
        names: dict[str, tuple[str, str | None]] = {}
        if obj_ids:
            for row in (
                db.query(ObjectType).filter(ObjectType.id.in_(obj_ids)).all()
            ):
                names[row.id] = (row.name, row.display_name)
        if rel_ids:
            rel_rows = (
                db.query(RelationType).filter(RelationType.id.in_(rel_ids)).all()
            )
            # 桥表以桥接对象的业务名为展示名：LLM 常把桥表关系的 display_name
            # 标成「属于」这类通用词，一屏「属于」无从区分；而每个桥表的 mapping_object
            # （如「银行交易」）才是该物化表的真实业务实体，用它既可区分又可精确搜索。
            mapping_ids = [
                r.mapping_object_type_id
                for r in rel_rows
                if r.mapping_object_type_id
            ]
            bridge_display = {
                o.id: o.display_name
                for o in db.query(ObjectType)
                .filter(ObjectType.id.in_(mapping_ids))
                .all()
            } if mapping_ids else {}
            for row in rel_rows:
                bridge_name = bridge_display.get(row.mapping_object_type_id)
                names[row.id] = (row.name, bridge_name or row.display_name)

        entities: list[dict] = []
        seen: set[str] = set()
        for r in rows:
            name, display = names.get(r["target_id"], (None, None))
            if not name or name in seen:  # 同名只留一个（重名=命名问题）
                continue
            seen.add(name)
            layer = r["target_layer"]
            entities.append(
                {
                    "name": name,
                    "display_name": display,
                    "kind": r["target_kind"],
                    "layer": layer,
                    "table": _auto_table_name(layer, name),
                }
            )
        entities.sort(key=lambda e: (e["kind"], e["name"]))
        result.append(
            {
                "ontology_id": ont.id,
                "domain_name": domain_names.get(domain_id, domain_id),
                "version": ont.version,
                "status": ont.status,
                "entities": entities,
            }
        )

    result.sort(key=lambda x: x["domain_name"])
    return {"ontologies": result}


@router.get(
    "/ontologies/{ontology_id}/materialization-contracts",
    response_model=list[MaterializationContractOut],
)
def list_materialization_contracts(
    ontology_id: str,
    target_kind: str | None = Query(
        None, description="object_type / relation_type / business_logic"
    ),
    materialized_only: bool = Query(False),
    db: Session = Depends(get_db),
):
    _require_ontology(db, ontology_id)
    contracts = materialization_contract_service.list_contracts(
        db,
        ontology_id,
        target_kind=target_kind,
        materialized_only=materialized_only,
    )
    names = materialization_contract_service.resolve_target_names(db, contracts)
    return [_to_out(c, names) for c in contracts]


@router.post(
    "/ontologies/{ontology_id}/materialization-contracts/sync",
    response_model=MaterializationContractSyncResult,
)
def sync_materialization_contracts(ontology_id: str, db: Session = Depends(get_db)):
    """按本体实体重新推导物化契约默认值。

    已存在的契约只更新**未被人工钉住**的字段——人工配置不会被机器覆盖。
    """
    _require_ontology(db, ontology_id)
    return materialization_contract_service.sync(db, ontology_id)


@router.patch(
    "/materialization-contracts/{contract_id}",
    response_model=MaterializationContractOut,
)
def update_materialization_contract(
    contract_id: str,
    payload: MaterializationContractUpdate,
    db: Session = Depends(get_db),
):
    """人工编辑。提交的字段会被钉住，此后机器推导不再覆盖。"""
    try:
        contract = materialization_contract_service.update(
            db, contract_id, payload.model_dump(exclude_unset=True)
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if contract is None:
        raise HTTPException(status_code=404, detail="物化契约不存在")
    names = materialization_contract_service.resolve_target_names(db, [contract])
    return _to_out(contract, names)


# ---------- Phase 2：业务源 → Flink → Doris ODS 接入契约 ----------


@router.get(
    "/ontologies/{ontology_id}/ingestion-contracts",
    response_model=list[IngestionContractOut],
)
def list_ingestion_contracts(ontology_id: str, db: Session = Depends(get_db)):
    _require_ontology(db, ontology_id)
    from app.services.ingestion_contract import IngestionContractService

    service = IngestionContractService()
    return [service.serialize(row) for row in service.list(db, ontology_id)]


@router.put(
    "/ontologies/{ontology_id}/ingestion-contracts",
    response_model=IngestionContractOut,
)
def upsert_ingestion_contract(
    ontology_id: str,
    payload: IngestionContractInput,
    db: Session = Depends(get_db),
):
    _require_ontology(db, ontology_id)
    from app.services.ingestion_contract import (
        IngestionContractError,
        IngestionContractService,
    )

    service = IngestionContractService()
    try:
        row = service.upsert(db, ontology_id, payload.model_dump())
    except IngestionContractError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return service.serialize(row)


@router.get("/ingestion-contracts/{contract_id}/health")
def ingestion_contract_health(contract_id: str, db: Session = Depends(get_db)):
    from app.services.flink_health import FlinkHealthError, check_ingestion_job

    try:
        return check_ingestion_job(db, contract_id)
    except FlinkHealthError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post(
    "/ingestion-contracts/{contract_id}/reconcile",
    response_model=IngestionContractOut,
)
def reconcile_ingestion_contract(
    contract_id: str,
    payload: IngestionTaskResultInput,
    db: Session = Depends(get_db),
):
    from app.services.ingestion_contract import (
        IngestionContractError,
        IngestionContractService,
    )

    service = IngestionContractService()
    try:
        row = service.reconcile_task_result(
            db,
            contract_id,
            task_state=payload.task_state,
            result=payload.result,
        )
    except IngestionContractError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return service.serialize(row)


# ---------- M3：本体 → 物理正向生成 ----------


@router.get("/warehouse/engines")
def list_warehouse_engines():
    """可用引擎及其能力矩阵。``verified=false`` 表示条目未逐项核实。"""
    return {
        "default": DEFAULT_ENGINE,
        "engines": [
            {
                "name": a.name,
                "implemented": not isinstance(a, UnimplementedAdapter),
                "capabilities": asdict(a.capabilities()),
            }
            for a in list_adapters()
        ],
    }


@router.get("/ontologies/{ontology_id}/warehouse/ddl")
def generate_warehouse_ddl(
    ontology_id: str,
    engine: str = Query(DEFAULT_ENGINE),
    database_prefix: str | None = Query(None, description="库名后缀，如 erp → dim_erp"),
    db: Session = Depends(get_db),
):
    _require_ontology(db, ontology_id)
    return warehouse_generator.generate_ddl(
        db, ontology_id, _require_engine(engine), database_prefix=database_prefix
    )


@router.get("/ontologies/{ontology_id}/warehouse/etl")
def generate_warehouse_etl(
    ontology_id: str,
    engine: str = Query(DEFAULT_ENGINE),
    database_prefix: str | None = Query(None),
    load_strategy: str | None = Query(
        None, description="full/incremental/cdc；缺省 full（INSERT OVERWRITE）"
    ),
    db: Session = Depends(get_db),
):
    _require_ontology(db, ontology_id)
    return warehouse_generator.generate_etl_sql(
        db,
        ontology_id,
        _require_engine(engine),
        database_prefix=database_prefix,
        load_strategy=load_strategy,
    )


@router.get("/ontologies/{ontology_id}/warehouse/dag")
def generate_warehouse_dag(
    ontology_id: str,
    database_prefix: str | None = Query(None),
    db: Session = Depends(get_db),
):
    """调度依赖 DAG。``cyclic`` 非空表示存在环，需人工拆环。"""
    _require_ontology(db, ontology_id)
    return warehouse_generator.generate_dag(
        db, ontology_id, database_prefix=database_prefix
    )


@router.get("/ontologies/{ontology_id}/warehouse/mapping")
def generate_warehouse_mapping(
    ontology_id: str,
    database_prefix: str | None = Query(None),
    db: Session = Depends(get_db),
):
    """物理映射，可直接写入 DataSource.mapping_json。"""
    _require_ontology(db, ontology_id)
    return warehouse_generator.generate_mapping(
        db, ontology_id, database_prefix=database_prefix
    )


@router.get("/ontologies/{ontology_id}/warehouse/derivation")
def generate_warehouse_derivation(
    ontology_id: str,
    engine: str = Query(..., description="目标引擎（非 hive）；hive 为权威源无需派生"),
    database_prefix: str | None = Query(None),
    db: Session = Depends(get_db),
):
    """从 Hive 权威副本派生到目标引擎的作业（单一写入路径）。

    Hive 权威写入，其余引擎从其派生，避免多副本双写不一致。
    """
    _require_ontology(db, ontology_id)
    return warehouse_generator.generate_derivation(
        db, ontology_id, _require_engine(engine), database_prefix=database_prefix
    )


@router.get("/ontologies/{ontology_id}/warehouse/bundle")
def generate_warehouse_bundle(
    ontology_id: str,
    engines: str = Query(DEFAULT_ENGINE, description="逗号分隔，如 hive,doris"),
    database_prefix: str | None = Query(None),
    db: Session = Depends(get_db),
):
    _require_ontology(db, ontology_id)
    wanted = [_require_engine(e.strip()) for e in engines.split(",") if e.strip()]
    return warehouse_generator.generate_bundle(
        db, ontology_id, wanted, database_prefix=database_prefix
    )


# ---------- M3+：本体一键物化（真正落库执行） ----------


def _materialize_out(artifact) -> MaterializeResult:
    import json

    receipt = None
    if artifact.execution_receipt_json:
        try:
            receipt = json.loads(artifact.execution_receipt_json)
        except (TypeError, ValueError):
            receipt = None
    return MaterializeResult(
        artifact_id=artifact.id,
        status=artifact.status,
        ok=bool(receipt and receipt.get("ok")),
        name=artifact.name or "物化",
        receipt=receipt,
        executed_at=artifact.executed_at,
        operator=artifact.confirmed_by,
        created_at=artifact.created_at,
    )


@router.post(
    "/ontologies/{ontology_id}/warehouse/materialize/preflight",
    response_model=MaterializePreflightResult,
)
def materialize_preflight(
    ontology_id: str,
    payload: MaterializePreflightRequest,
    db: Session = Depends(get_db),
):
    """物化提交前自检：把「三分钟后才在任务日志里知道」的一类失败提到点提交之前。

    **只读**：不落产物、不触发运行，可随便重跑。逐项返回 ``status``（pass/warn/fail）与
    失败时可照做的 ``next_step``；前端据 ``ok``（无阻断失败）决定是否放行「提交」。
    覆盖 Airflow 可达/鉴权/REST 版本/建表与 Flink Connection/SSH 投递目录/批次规模，
    并在同步任务上检查 Flink JAR、checkpoint 等执行前置条件。
    """
    _require_ontology(db, ontology_id)
    _require_new_warehouse_engine(payload.engine)

    from app.services.materialize_preflight import run_preflight

    report = run_preflight(
        db,
        ontology_id,
        target_datasource_id=payload.target_datasource_id,
        engine=payload.engine,
        selected_targets=payload.selected_targets,
        managed_connections=True,
    )
    return MaterializePreflightResult(
        ok=report.ok,
        items=[
            {
                "key": i.key,
                "label": i.label,
                "status": i.status,
                "blocking": i.blocking,
                "detail": i.detail,
                "next_step": i.next_step,
            }
            for i in report.items
        ],
    )


@router.post(
    "/ontologies/{ontology_id}/warehouse/materialize",
    response_model=MaterializeResult,
)
def materialize_ontology(
    ontology_id: str,
    payload: MaterializeRequest,
    db: Session = Depends(get_db),
):
    """把本体物化到默认 Doris：生成 DDL 并提交 Airflow 建表任务。

    可对当前工作本体（草稿或已发布）执行——门槛仅在于 ``publisher`` 角色与所选目标库；
    归档本体不可物化。弹窗即人工确认面，故直接置 ``confirmed`` 后执行（物化非高危，无
    dry-run 门禁）；全程复用治理制品流水线，产出可审计的执行回执。执行失败不抛 5xx——
    回执里带 ``ok=false`` 与逐条错误，由前端呈现。
    """
    ontology = _require_ontology(db, ontology_id)
    if ontology.status == OntologyStatus.ARCHIVED.value:
        raise HTTPException(status_code=400, detail="归档本体不可物化")
    _require_new_warehouse_engine(payload.engine)

    context = {
        "ontology_id": ontology_id,
        "target_datasource_id": payload.target_datasource_id,
        "engine": payload.engine,
        "database_prefix": payload.database_prefix,
        "database_overrides": payload.database_overrides,
        "table_overrides": payload.table_overrides,
        "selected_targets": payload.selected_targets,
        "overrides": payload.overrides,
    }
    try:
        artifact = agent_pipeline.draft(
            db,
            kind="materialize",
            intent=payload.intent or f"物化 → {payload.engine}",
            context=context,
            ontology_id=ontology_id,
        )
    except ValueError as exc:  # 缺 target_datasource_id 等输入问题
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    artifact.status = ArtifactStatus.CONFIRMED.value
    artifact.confirmed_by = payload.operator
    artifact.confirmed_at = datetime.now(timezone.utc)
    artifact.origin = "user"
    artifact.user_created = True
    db.commit()

    # 制品 id 进 context：orchestrated 用它作 DagRun 的确定性 run_id，重复提交即幂等。
    artifact = agent_pipeline.execute(
        db, artifact.id, context={**context, "artifact_id": artifact.id}
    )
    return _materialize_out(artifact)


def _receipt_batches(db: Session, artifact_id: str) -> list[dict]:
    """读取物化制品回执里的批次列表。"""
    import json

    from app.models.agent import GovernanceArtifact

    artifact = db.get(GovernanceArtifact, artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="物化制品不存在")
    try:
        receipt = json.loads(artifact.execution_receipt_json or "{}")
    except (TypeError, ValueError):
        receipt = {}
    batches = receipt.get("batches") or []
    if not any(b.get("dag_id") and b.get("dag_run_id") for b in batches):
        raise HTTPException(status_code=400, detail="回执里没有 DagRun 信息，可能提交未成功")
    return batches


@router.get("/warehouse/materialize/{artifact_id}/tasks/{task_id}/result")
def get_materialize_task_result(
    artifact_id: str, task_id: str, db: Session = Depends(get_db)
):
    """一个搬运任务的执行结果：搬了多少行、水位到哪。

    值来自该任务的 XCom。当前同步任务统一由 Flink SQL 执行，回执不再携带执行档位。

    **按需读，不进轮询**：Airflow 没有跨任务批量读 XCom 的端点，一次一请求；整轮几百个
    任务在 5 秒一次的状态轮询里全读一遍会把 Airflow 打垮。故由前端在展开单个任务时调。
    """
    from app.connectors.airflow import AirflowClient, AirflowError

    batches = _receipt_batches(db, artifact_id)
    import json
    from app.models.agent import GovernanceArtifact

    artifact = db.get(GovernanceArtifact, artifact_id)
    try:
        artifact_receipt = json.loads(artifact.execution_receipt_json or "{}") if artifact else {}
    except (TypeError, ValueError):
        artifact_receipt = {}
    ingestion_contract_id = artifact_receipt.get("ingestion_contract_id")
    airflow = settings_service.get_airflow_runtime(db)
    client = AirflowClient(
        airflow.endpoint,
        username=airflow.username,
        password=airflow.password,
    )
    # 任务落在哪一批不由前端记：逐批试，命中即返回。批数是个位到十位数，代价可接受。
    try:
        for b in batches:
            bid, brun = b.get("dag_id"), b.get("dag_run_id")
            if not bid or not brun:
                continue
            try:
                value = client.get_xcom(bid, brun, task_id)
            except AirflowError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
            if value is None:
                continue
            task_state = None
            try:
                task_state = next(
                    (
                        item.get("state")
                        for item in client.list_task_instances(bid, brun)
                        if item.get("task_id") == task_id
                    ),
                    None,
                )
            except (AirflowError, AttributeError):
                task_state = None
            if not isinstance(value, dict):
                return {
                    "task_id": task_id,
                    "dag_id": bid,
                    "task_state": task_state,
                    "raw": value,
                }
            ingestion_status = None
            if ingestion_contract_id and task_state:
                from app.services.ingestion_contract import IngestionContractService

                contract = IngestionContractService().reconcile_task_result(
                    db,
                    ingestion_contract_id,
                    task_state=task_state,
                    result=value,
                )
                ingestion_status = contract.status
            return {
                "task_id": task_id,
                "dag_id": bid,
                "task_state": task_state,
                "ingestion_status": ingestion_status,
                "job_id": value.get("flink_job_id") or value.get("job_id"),
                "rows_read": value.get("rows_read"),
                "rows_written": value.get("rows_written"),
                "watermark_after": value.get("watermark_after"),
            }
    finally:
        client.close()
    # 没有值 ≠ 出错：任务还没跑完、或它是建表/切换任务（不产 XCom）。
    return {"task_id": task_id, "dag_id": None}


@router.get("/warehouse/materialize/{artifact_id}/status")
def get_materialize_status(artifact_id: str, db: Session = Depends(get_db)):
    """回读一次编排物化的运行状态（供弹窗轮询）。

    **只读**：状态的权威在 Airflow，这里不缓存、不改制品——回执记录的是「提交了什么」，
    运行到哪一步随时可能变，缓存一份只会出现两个互相矛盾的事实。

    M16：一次物化可产多个 DAG（按 cron 分组 + 分批）。逐个回读 DagRun 并聚合出整轮状态，
    同时返回 ``batches`` 明细供弹窗按批展示。
    """
    from app.connectors.airflow import AirflowClient, AirflowError, is_terminal

    batches = _receipt_batches(db, artifact_id)
    airflow = settings_service.get_airflow_runtime(db)
    client = AirflowClient(
        airflow.endpoint,
        username=airflow.username,
        password=airflow.password,
    )

    def _tasks(items):
        return [
            {
                "task_id": t.get("task_id"),
                "state": t.get("state"),
                "try_number": t.get("try_number"),
            }
            for t in items
        ]

    batch_out: list[dict] = []
    all_tasks: list[dict] = []
    try:
        for b in batches:
            bid, brun = b.get("dag_id"), b.get("dag_run_id")
            # 触发就失败的批（如未解析到 DAG）没有真实 DagRun 可读，用回执里的状态。
            if b.get("error") or not bid or not brun:
                st = b.get("state") or "failed"
                batch_out.append(
                    {
                        "suffix": b.get("suffix"),
                        "dag_id": bid,
                        "dag_run_id": brun,
                        "state": st,
                        "terminal": is_terminal(st),
                        "run_url": b.get("run_url"),
                        "error": b.get("error"),
                        "tasks": [],
                    }
                )
                continue
            try:
                run = client.get_dag_run(bid, brun)
                tasks = _tasks(client.list_task_instances(bid, brun))
            except AirflowError as exc:
                # 单批读不到不整体 502：可能刚触发还没落库，标 unknown 让前端继续轮询。
                batch_out.append(
                    {
                        "suffix": b.get("suffix"),
                        "dag_id": bid,
                        "dag_run_id": brun,
                        "state": None,
                        "terminal": False,
                        "run_url": client.run_url(bid, brun),
                        "error": str(exc),
                        "tasks": [],
                    }
                )
                continue
            st = run.get("state")
            batch_out.append(
                {
                    "suffix": b.get("suffix"),
                    "dag_id": bid,
                    "dag_run_id": brun,
                    "state": st,
                    "terminal": is_terminal(st),
                    "run_url": client.run_url(bid, brun),
                    "start_date": run.get("start_date"),
                    "end_date": run.get("end_date"),
                    "tasks": tasks,
                }
            )
            all_tasks.extend(tasks)
    finally:
        client.close()

    states = [b["state"] for b in batch_out]
    agg = _aggregate_state(states)
    first = batch_out[0]
    return {
        "artifact_id": artifact_id,
        # 顶层摘要指向首批，完整运行信息在 batches。
        "dag_id": first.get("dag_id"),
        "dag_run_id": first.get("dag_run_id"),
        "state": agg,
        "terminal": all(b["terminal"] for b in batch_out),
        "run_url": first.get("run_url"),
        "tasks": all_tasks,
        "batches": batch_out,
    }


def _aggregate_state(states: list) -> str | None:
    """多批 DagRun 状态 → 整轮状态。任一失败即失败；全成功才成功；否则取「还在跑」。"""
    present = [s for s in states if s]
    if not present:
        return None
    if any(s in ("failed", "upstream_failed") for s in present):
        return "failed"
    if all(s == "success" for s in states):
        return "success"
    if any(s == "running" for s in present):
        return "running"
    if any(s in ("queued", "scheduled") for s in present):
        return "queued"
    return present[0]


@router.get(
    "/ontologies/{ontology_id}/warehouse/materialization-runs",
    response_model=list[MaterializeResult],
)
def list_materialization_runs(ontology_id: str, db: Session = Depends(get_db)):
    """本体的历次物化执行记录（含回执），供面板展示已物化状态。最新在前。"""
    runs = agent_pipeline.list_artifacts(
        db, kind="materialize", ontology_id=ontology_id
    )
    return [_materialize_out(a) for a in runs]


# ---------- M7：本体 → DataHub 回写 ----------

@router.get("/ontologies/{ontology_id}/datahub/writeback-plan")
def datahub_writeback_plan(ontology_id: str, db: Session = Depends(get_db)):
    """列出将要回写 DataHub 的变更。纯读，不触碰 DataHub。"""
    try:
        return datahub_writeback.build_plan(db, ontology_id).to_dict()
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/ontologies/{ontology_id}/datahub/writeback")
def datahub_writeback_apply(ontology_id: str, db: Session = Depends(get_db)):
    """把业务命名/描述/术语/域回灌 DataHub。

    仅已发布本体可回写；空值不覆盖 DataHub 已有内容。
    """
    try:
        return datahub_writeback.apply_sync(db, ontology_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ---------- M11：物化血缘兜底上报 ----------

def _lineage_kwargs_from_artifact(artifact) -> dict:
    """从物化制品的 spec 重建血缘计划入参。

    血缘要与**实际物化的那一次**对应，故直接用制品落盘的 spec（引擎/库表
    覆盖/勾选实体），而不是重新算一遍参数——后者可能与当时不一致。
    """
    import json

    try:
        spec = json.loads(artifact.spec_json or "{}")
    except (TypeError, ValueError):
        spec = {}
    return {
        "engine": spec.get("engine") or "hive",
        "database_prefix": spec.get("database_prefix"),
        "database_overrides": spec.get("database_overrides"),
        "table_overrides": spec.get("table_overrides"),
        "selected_targets": spec.get("selected_targets"),
    }


def _lineage_artifact(db: Session, artifact_id: str):
    from app.models.agent import GovernanceArtifact

    artifact = db.get(GovernanceArtifact, artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="物化制品不存在")
    if artifact.kind != "materialize":
        raise HTTPException(status_code=400, detail="该制品不是物化制品，无血缘可上报")
    ontology_id = None
    try:
        import json

        ontology_id = (json.loads(artifact.spec_json or "{}") or {}).get("ontology_id")
    except (TypeError, ValueError):
        pass
    ontology_id = ontology_id or artifact.ontology_id
    if not ontology_id:
        raise HTTPException(status_code=400, detail="制品未关联本体，无法上报血缘")
    return artifact, ontology_id


@router.get("/warehouse/materialize/{artifact_id}/lineage-plan")
def materialize_lineage_plan(artifact_id: str, db: Session = Depends(get_db)):
    """列出本次物化将上报的 源表→目标表 血缘。纯读，不触碰 DataHub。"""
    artifact, ontology_id = _lineage_artifact(db, artifact_id)
    plan = lineage_emitter.build_plan(
        db, ontology_id, **_lineage_kwargs_from_artifact(artifact)
    )
    return plan.to_dict()


@router.post("/warehouse/materialize/{artifact_id}/lineage")
def materialize_lineage_apply(artifact_id: str, db: Session = Depends(get_db)):
    """兜底上报本次物化的表级血缘到 DataHub。

    主路径是 Airflow 插件自动上报；插件缺位/未接入时用本端口补。
    两条路径产同一份 URN，重复上报幂等。
    """
    artifact, ontology_id = _lineage_artifact(db, artifact_id)
    return lineage_emitter.apply_sync(
        db, ontology_id, **_lineage_kwargs_from_artifact(artifact)
    )
