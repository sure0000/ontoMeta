import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import edit_service, settings_service, workspace
from app.database import get_db
from app.models import ObjectType
from app.schemas import (
    ChangeLogOut,
    DataHubDatasetOption,
    DomainContextDetail,
    DomainContextSummary,
    DraftProgressOut,
    EnsureObjectTypeRequest,
    ObjectTypeSummary,
    TaskRecordOut,
)
from app.services.draft_generation_queue import run_draft_generation_limited
from app.services.query import DraftGenerationAlreadyRunning, WorkspaceService

router = APIRouter()

@router.get("/datahub/datasets", response_model=list[DataHubDatasetOption])
async def search_datahub_datasets(
    query: str = Query(""),
    ontology_id: str | None = Query(None),
    db: Session = Depends(get_db),
):
    """搜索 DataHub datasets。

    若提供 ontology_id，会在结果中标注该 dataset 是否已映射为本体下的 ObjectType。
    """
    from app.connectors.datahub import DataHubConnector

    connector = DataHubConnector(settings_service.get_datahub_runtime(db))
    try:
        datasets = await connector.search_datasets(query)
    finally:
        await connector.aclose()

    options: list[DataHubDatasetOption] = []
    for ds in datasets:
        object_type_id = None
        object_type_display_name = None
        if ontology_id:
            existing = (
                db.query(ObjectType)
                .filter(
                    ObjectType.ontology_id == ontology_id,
                    ObjectType.source_ref == ds.urn,
                )
                .first()
            )
            if existing:
                object_type_id = existing.id
                object_type_display_name = existing.display_name
        options.append(
            DataHubDatasetOption(
                urn=ds.urn,
                name=ds.name,
                display_name=ds.display_name,
                description=ds.description,
                platform=ds.platform,
                container=ds.container,
                object_type_id=object_type_id,
                object_type_display_name=object_type_display_name,
                datahub_url=connector.get_dataset_url(ds.urn),
            )
        )
    return options


@router.post("/object-types/ensure", response_model=ObjectTypeSummary)
async def ensure_object_type_from_dataset(
    data: EnsureObjectTypeRequest,
    db: Session = Depends(get_db),
):
    """根据 DataHub dataset urn 查找或创建对应 ObjectType。"""
    try:
        return await edit_service.ensure_object_type_from_dataset(
            db,
            ontology_id=data.ontology_id,
            dataset_urn=data.dataset_urn,
            operator=data.operator,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/domains", response_model=list[DomainContextSummary])
async def list_domains(db: Session = Depends(get_db)):
    try:
        return await workspace.sync_domains(db)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"无法从 DataHub 同步数据域，请检查 DataHub 连接配置：{exc}",
        ) from exc


@router.get("/domains/{domain_id}", response_model=DomainContextDetail)
def get_domain(domain_id: str, db: Session = Depends(get_db)):
    detail = workspace.get_domain(db, domain_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Domain not found")
    return detail


def _launch_draft_task(progress: DraftProgressOut, runner) -> None:
    """把某个范围的生成执行函数挂到并发限流队列上并跟踪其 asyncio 任务。"""

    async def _execute() -> None:
        await runner(progress.task_id)

    task = asyncio.create_task(
        run_draft_generation_limited(
            progress.task_id,
            WorkspaceService._update_task_progress,
            _execute,
            WorkspaceService._is_task_cancelled,
        )
    )
    workspace._track_draft_task(progress.task_id, task)


@router.post("/domains/{domain_id}/generate-draft", response_model=DraftProgressOut)
async def generate_draft(domain_id: str, db: Session = Depends(get_db)):
    try:
        progress = workspace.start_draft_generation(db, domain_id)
        _launch_draft_task(
            progress, lambda task_id: workspace._run_draft_generation(domain_id, task_id)
        )
        return progress
    except DraftGenerationAlreadyRunning as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/domains/{domain_id}/generate-objects", response_model=DraftProgressOut)
async def generate_objects(domain_id: str, db: Session = Depends(get_db)):
    """仅生成业务对象；可与 /generate-relations 并行触发，互不阻塞。"""
    try:
        progress = workspace.start_object_generation(db, domain_id)
        _launch_draft_task(
            progress, lambda task_id: workspace._run_object_generation(domain_id, task_id)
        )
        return progress
    except DraftGenerationAlreadyRunning as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/domains/{domain_id}/generate-relations", response_model=DraftProgressOut)
async def generate_relations(domain_id: str, db: Session = Depends(get_db)):
    """仅生成业务关系；需已存在含业务对象的草稿本体，可与 /generate-objects 并行触发。"""
    try:
        progress = workspace.start_relation_generation(db, domain_id)
        _launch_draft_task(
            progress, lambda task_id: workspace._run_relation_generation(domain_id, task_id)
        )
        return progress
    except DraftGenerationAlreadyRunning as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/domains/{domain_id}/progress", response_model=DraftProgressOut)
def get_progress(
    domain_id: str,
    scope: str | None = Query(None, description="按范围过滤：full/objects/relations"),
    db: Session = Depends(get_db),
):
    progress = workspace.get_progress(db, domain_id, scope=scope)
    if not progress:
        raise HTTPException(status_code=404, detail="No generation task found")
    return progress


@router.get("/domains/{domain_id}/tasks", response_model=list[TaskRecordOut])
def list_domain_tasks(domain_id: str, db: Session = Depends(get_db)):
    domain = workspace.get_domain(db, domain_id)
    if not domain:
        raise HTTPException(status_code=404, detail="Domain not found")
    return workspace.list_tasks(db, domain_id)


@router.get("/domains/{domain_id}/tasks/{task_id}/logs", response_model=list[ChangeLogOut])
def get_task_logs(domain_id: str, task_id: str, db: Session = Depends(get_db)):
    try:
        return workspace.get_task_logs(db, domain_id, task_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/domains/{domain_id}/tasks/{task_id}/stop", response_model=TaskRecordOut)
def stop_draft_task(domain_id: str, task_id: str, db: Session = Depends(get_db)):
    try:
        return workspace.stop_draft_generation(db, domain_id, task_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


_RETRY_RUNNERS = {
    "objects": lambda domain_id, task_id: workspace._run_object_generation(domain_id, task_id),
    "relations": lambda domain_id, task_id: workspace._run_relation_generation(domain_id, task_id),
    "full": lambda domain_id, task_id: workspace._run_draft_generation(domain_id, task_id),
}


@router.post("/domains/{domain_id}/tasks/{task_id}/retry", response_model=DraftProgressOut)
async def retry_draft_task(domain_id: str, task_id: str, db: Session = Depends(get_db)):
    try:
        progress = workspace.retry_draft_generation(db, domain_id, task_id)
        runner = _RETRY_RUNNERS.get(progress.scope, _RETRY_RUNNERS["full"])
        _launch_draft_task(progress, lambda tid: runner(domain_id, tid))
        return progress
    except DraftGenerationAlreadyRunning as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/domains/{domain_id}/draft-duplicates")
def get_draft_duplicates(domain_id: str, db: Session = Depends(get_db)):
    domain = workspace.get_domain(db, domain_id)
    if not domain:
        raise HTTPException(status_code=404, detail="Domain not found")
    return workspace.report_duplicate_drafts(db, domain_id)
