"""依赖组件统一部署管理路由（DEPENDENCY_DEPLOYMENT_REDESIGN Phase 0）。

每个依赖组件在设置页选一种部署方式（已有/Docker/K8s/物理机），部署成功自动回写连接，
或选「已有」手填连接。本组路由独立于既有 /settings/llm-services 等（Phase 1 起读取侧
改为从本表投影，旧路由转薄层）。
"""

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas import (
    DependencyComponentCreate,
    DependencyComponentOut,
    DependencyComponentUpdate,
    DependencySchemaOut,
    DeployResultOut,
    ProbeResultOut,
)
from app.services.dependency_service import DependencyComponentService

router = APIRouter()
_service = DependencyComponentService()


@router.get("/settings/dependencies/schema", response_model=DependencySchemaOut)
def get_dependency_schema():
    """组件目录 + 连接/部署 schema 自描述，供前端表单生成。"""
    return _service.schema()


@router.get("/settings/dependencies", response_model=list[DependencyComponentOut])
def list_dependencies(db: Session = Depends(get_db)):
    return [_service.to_out(r) for r in _service.list_components(db)]


@router.post("/settings/dependencies", response_model=DependencyComponentOut)
def create_dependency(data: DependencyComponentCreate, db: Session = Depends(get_db)):
    try:
        row = _service.create_component(db, data.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _service.to_out(row)


@router.get("/settings/dependencies/{component_id}", response_model=DependencyComponentOut)
def get_dependency(component_id: str, db: Session = Depends(get_db)):
    row = _service.get_component(db, component_id)
    if not row:
        raise HTTPException(status_code=404, detail="依赖组件不存在")
    return _service.to_out(row)


@router.put("/settings/dependencies/{component_id}", response_model=DependencyComponentOut)
def update_dependency(
    component_id: str, data: DependencyComponentUpdate, db: Session = Depends(get_db)
):
    try:
        row = _service.update_component(db, component_id, data.model_dump(exclude_unset=True))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not row:
        raise HTTPException(status_code=404, detail="依赖组件不存在")
    return _service.to_out(row)


@router.delete("/settings/dependencies/{component_id}")
def delete_dependency(component_id: str, db: Session = Depends(get_db)):
    if not _service.delete_component(db, component_id):
        raise HTTPException(status_code=404, detail="依赖组件不存在")
    return {"id": component_id, "deleted": True}


@router.post("/settings/dependencies/{component_id}/probe", response_model=ProbeResultOut)
def probe_dependency(component_id: str, db: Session = Depends(get_db)):
    """拨测当前连接信息（按组件类型分派，复用既有 LLM/Airflow/SQLAlchemy 拨测）。"""
    result = _service.probe(db, component_id)
    return ProbeResultOut(ok=result.ok, message=result.message, latency_ms=result.latency_ms)


@router.post("/settings/dependencies/{component_id}/deploy", response_model=DeployResultOut)
def deploy_dependency(
    component_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """执行部署。external 同步拨测直接返回；docker/k8s/bare_metal（尤其 SSH 安装可能
    持续数分钟）先置 deploying 立即返回，实际部署交后台执行，前端轮询组件状态。"""
    try:
        result = _service.start_deploy(db, component_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if result.pop("need_background", False):
        background_tasks.add_task(_service.run_deploy_detached, component_id)
    return DeployResultOut(**result)


@router.post("/settings/dependencies/{component_id}/teardown")
def teardown_dependency(component_id: str, db: Session = Depends(get_db)):
    try:
        return _service.teardown(db, component_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
