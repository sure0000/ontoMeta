"""依赖组件登记路由：固定组件的连接信息 + 拨测。

ontoMeta 只连接已经跑着的服务，所以这里没有创建/删除/部署——组件由
``ensure_components`` 兜底补齐，面板只能编辑连接和拨测。
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas import (
    DependencyComponentOut,
    DependencyComponentUpdate,
    DependencySchemaOut,
    ProbeResultOut,
)
from app.services.dependency_service import DependencyComponentService

router = APIRouter()
_service = DependencyComponentService()


@router.get("/settings/dependencies/schema", response_model=DependencySchemaOut)
def get_dependency_schema():
    """组件目录 + 连接 schema 自描述，供前端表单生成。"""
    return _service.schema()


@router.get("/settings/dependencies", response_model=list[DependencyComponentOut])
def list_dependencies(db: Session = Depends(get_db)):
    # 组件不由用户新增，缺的行在这里补齐——否则新库里面板是空的，而用户没有"新增"可点。
    _service.ensure_components(db)
    return [_service.to_out(r) for r in _service.list_components(db)]


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


@router.post("/settings/dependencies/{component_id}/probe", response_model=ProbeResultOut)
def probe_dependency(
    component_id: str, target: str | None = None, db: Session = Depends(get_db)
):
    """拨测当前连接信息（按组件类型分派）。

    ``target``：只测某一条连接（Airflow 的 ``api`` / ``ssh``），省略则全测。一个组件的
    几条连接互不相干，得能分开测——否则 SSH 没配好会把「调度 API 其实是通的」也盖掉。
    """
    result = _service.probe(db, component_id, target=target)
    return ProbeResultOut(
        ok=result.ok, message=result.message, latency_ms=result.latency_ms, parts=result.parts
    )
