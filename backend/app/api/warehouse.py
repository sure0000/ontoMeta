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
    materialization_contract_service,
    settings_service,
    warehouse_generator,
)
from app.database import get_db
from app.models import MaterializationContract, Ontology
from app.models.agent import ArtifactStatus
from app.models.ontology import OntologyStatus
from app.schemas import (
    MaterializationContractOut,
    MaterializationContractSyncResult,
    MaterializationContractUpdate,
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

router = APIRouter()


def _require_ontology(db: Session, ontology_id: str) -> Ontology:
    ontology = db.query(Ontology).filter(Ontology.id == ontology_id).first()
    if ontology is None:
        raise HTTPException(status_code=404, detail="本体不存在")
    return ontology


def _require_engine(engine: str) -> str:
    try:
        get_adapter(engine)
    except UnknownEngineError:
        raise HTTPException(
            status_code=400,
            detail=f"未知引擎 {engine}，可选：{', '.join(list_engines())}",
        ) from None
    return engine


def _to_out(
    contract: MaterializationContract,
    names: dict[str, tuple[str | None, str | None]],
) -> MaterializationContractOut:
    out = MaterializationContractOut.model_validate(contract)
    name, display_name = names.get(contract.target_id, (None, None))
    out.target_name = name
    out.target_display_name = display_name
    return out


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
    contract = materialization_contract_service.update(
        db, contract_id, payload.model_dump(exclude_unset=True)
    )
    if contract is None:
        raise HTTPException(status_code=404, detail="物化契约不存在")
    names = materialization_contract_service.resolve_target_names(db, [contract])
    return _to_out(contract, names)


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
    "/ontologies/{ontology_id}/warehouse/materialize",
    response_model=MaterializeResult,
)
def materialize_ontology(
    ontology_id: str,
    payload: MaterializeRequest,
    db: Session = Depends(get_db),
):
    """把本体物化到目标数据源：生成 DDL/ETL 并真正建表落数。

    可对当前工作本体（草稿或已发布）执行——门槛仅在于 ``publisher`` 角色与所选目标库；
    归档本体不可物化。弹窗即人工确认面，故直接置 ``confirmed`` 后执行（物化非高危，无
    dry-run 门禁）；全程复用治理制品流水线，产出可审计的执行回执。执行失败不抛 5xx——
    回执里带 ``ok=false`` 与逐条错误，由前端呈现。
    """
    ontology = _require_ontology(db, ontology_id)
    if ontology.status == OntologyStatus.ARCHIVED.value:
        raise HTTPException(status_code=400, detail="归档本体不可物化")
    _require_engine(payload.engine)

    context = {
        "ontology_id": ontology_id,
        "target_datasource_id": payload.target_datasource_id,
        "engine": payload.engine,
        "database_prefix": payload.database_prefix,
        "database_overrides": payload.database_overrides,
        "table_overrides": payload.table_overrides,
        "load_strategy": payload.load_strategy,
        "selected_targets": payload.selected_targets,
        "overrides": payload.overrides,
        "execute_mode": payload.execute_mode,
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


@router.get("/warehouse/materialize/{artifact_id}/status")
def get_materialize_status(artifact_id: str, db: Session = Depends(get_db)):
    """回读一次编排物化的运行状态（供弹窗轮询）。

    **只读**：状态的权威在 Airflow，这里不缓存、不改制品——回执记录的是「提交了什么」，
    运行到哪一步随时可能变，缓存一份只会出现两个互相矛盾的事实。
    """
    import json

    from app.connectors.airflow import AirflowClient, AirflowError, is_terminal
    from app.models.agent import GovernanceArtifact

    artifact = db.get(GovernanceArtifact, artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="物化制品不存在")
    try:
        receipt = json.loads(artifact.execution_receipt_json or "{}")
    except (TypeError, ValueError):
        receipt = {}
    if receipt.get("execute_mode") != "orchestrated":
        raise HTTPException(
            status_code=400, detail="该物化为直连执行（开发模式），无调度运行状态可查"
        )

    dag_id, run_id = receipt.get("dag_id"), receipt.get("dag_run_id")
    if not dag_id or not run_id:
        raise HTTPException(status_code=400, detail="回执里没有 DagRun 信息，可能提交未成功")

    airflow = settings_service.get_airflow_runtime(db)
    client = AirflowClient(
        airflow.endpoint,
        username=airflow.username,
        password=airflow.password,
        token=airflow.token,
        api_version=airflow.api_version,
    )
    try:
        run = client.get_dag_run(dag_id, run_id)
        tasks = client.list_task_instances(dag_id, run_id)
    except AirflowError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        client.close()

    state = run.get("state")
    return {
        "artifact_id": artifact_id,
        "dag_id": dag_id,
        "dag_run_id": run_id,
        "state": state,
        "terminal": is_terminal(state),
        "start_date": run.get("start_date"),
        "end_date": run.get("end_date"),
        "run_url": client.run_url(dag_id, run_id),
        "tasks": [
            {
                "task_id": t.get("task_id"),
                "state": t.get("state"),
                "try_number": t.get("try_number"),
            }
            for t in tasks
        ],
    }


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
