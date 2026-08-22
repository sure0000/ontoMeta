import asyncio
import os
import subprocess
import sys
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import edit_service, provenance_service, settings_service, workspace
from app.config import settings
from app.database import get_db
from app.models import ObjectType
from app.schemas import (
    ChangeLogOut,
    DataHubDatasetOption,
    DomainContextDetail,
    DomainContextSummary,
    DraftProgressOut,
    EnsureObjectTypeRequest,
    ManualObjectCreateRequest,
    ManualObjectCreateResponse,
    MergeReportOut,
    ObjectTypeSummary,
    TaskRecordOut,
)
from app.services.draft_generation_queue import run_draft_generation_limited
from app.services.draft_generator import LlmNotConfiguredError
from app.services.manual_creation import ManualCreationService, ManualPropertyInput
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

    runtime = settings_service.get_datahub_runtime(db)
    if not runtime.gms_url:
        raise HTTPException(
            status_code=503,
            detail="未配置 DataHub GMS 地址，请在「设置 → DataHub」中填写后重试。",
        )
    connector = DataHubConnector(runtime)
    try:
        datasets = await connector.search_datasets(query)
    except httpx.ConnectError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"DataHub 不可达（{runtime.gms_url}）：{exc}。请检查 GMS 地址是否可从后端访问。",
        ) from exc
    except httpx.TransportError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"DataHub 连接异常（{runtime.gms_url}）：{exc}",
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=f"DataHub 查询失败：{exc}") from exc
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
    except httpx.ConnectError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"DataHub 不可达：{exc}。请检查设置中的 GMS 地址是否可从后端访问。",
        ) from exc
    except httpx.TransportError as exc:
        raise HTTPException(status_code=503, detail=f"DataHub 连接异常：{exc}") from exc


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


# backend 根目录（含 app/ 包），供子进程 cwd 与日志目录定位。
_BACKEND_DIR = Path(__file__).resolve().parents[2]
_LOG_DIR = _BACKEND_DIR.parent / ".logs"


def _spawn_draft_worker(task_id: str) -> None:
    """在分离子进程执行草稿生成（C）：start_new_session=True 使其脱离 uvicorn 的
    进程组，``--reload`` 重启或异常退出杀 API worker 时不会波及生成任务。"""
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_path = _LOG_DIR / f"draft-worker-{task_id}.log"
        logfile = open(log_path, "ab", buffering=0)  # noqa: SIM115 —— 交由子进程持有
    except OSError:
        logfile = subprocess.DEVNULL

    try:
        subprocess.Popen(
            [sys.executable, "-m", "app.jobs.draft_worker", task_id],
            cwd=str(_BACKEND_DIR),
            stdout=logfile,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=os.environ.copy(),
        )
    finally:
        # 父进程立即关闭自己的文件句柄；子进程已继承独立副本。
        if logfile not in (subprocess.DEVNULL, None):
            logfile.close()


def _launch_draft_task(progress: DraftProgressOut, runner) -> None:
    """派发某个范围的生成执行。

    默认（``draft_worker_subprocess=True``）走分离子进程，reload 免疫；否则回退到进程内
    asyncio 限流队列（测试/inline）。两条路径的进度/状态/取消都经由 DB，语义一致。
    """
    if settings.draft_worker_subprocess:
        _spawn_draft_worker(progress.task_id)
        return

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
    except LlmNotConfiguredError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
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
    except LlmNotConfiguredError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
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
    except LlmNotConfiguredError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except DraftGenerationAlreadyRunning as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


_manual_creation = ManualCreationService()


@router.post(
    "/domains/{domain_id}/manual/object-types",
    response_model=ManualObjectCreateResponse,
)
def create_manual_object(
    domain_id: str,
    data: ManualObjectCreateRequest,
    db: Session = Depends(get_db),
):
    """人工生成：手工定义一个业务对象写入数据域草稿本体，并按数据源方言生成建表 DDL。

    与「从 DataHub 预生成」互补：预生成面向存量/历史数据的本体治理，本接口
    面向新业务的“本体先行”建模（先定义本体，再据此在数据源上建物理表）。
    """
    try:
        result = _manual_creation.create_object(
            db,
            domain_id,
            name=data.name,
            display_name=data.display_name,
            description=data.description,
            dialect=data.dialect,
            data_source=data.data_source,
            properties=[
                ManualPropertyInput(
                    name=p.name,
                    display_name=p.display_name,
                    data_type=p.data_type,
                    semantic_type=p.semantic_type,
                    required=p.required,
                    primary_key=p.primary_key,
                )
                for p in data.properties
            ],
        )
        return ManualObjectCreateResponse(
            ontology_id=result.ontology_id,
            object_type_id=result.object_type_id,
            table_name=result.table_name,
            ddl=result.ddl,
        )
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
    except LlmNotConfiguredError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except DraftGenerationAlreadyRunning as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/domains/{domain_id}/discard-unpublished")
def discard_unpublished(domain_id: str, db: Session = Depends(get_db)):
    """丢弃工作本体里从未发布过的实体，回到「只剩已发布内容」。已发布内容不动。"""
    from app.services import ontology_workspace

    try:
        return ontology_workspace.discard_unpublished(db, domain_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get(
    "/domains/{domain_id}/tasks/{task_id}/merge-report",
    response_model=MergeReportOut,
)
def get_task_merge_report(domain_id: str, task_id: str, db: Session = Depends(get_db)):
    """返回某次生成运行的合并变更报告（新增/更新/保留/冲突/上游删除）。"""
    try:
        return provenance_service.get_merge_report(db, domain_id, task_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


