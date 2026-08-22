"""维度模型 API。"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field

from app.database import get_db
from app.services.dimensional_model import DimensionalModelService

router = APIRouter(prefix="/dimensional-models", tags=["dimensional-models"])
dim_model_service = DimensionalModelService()


class CreateDimensionalModelRequest(BaseModel):
    modeling_case_id: str | None = Field(None, description="关联的建模工单 ID")
    domain_id: str = Field(..., description="数据域 ID")
    ontology_id: str = Field(..., description="本体 ID")
    name: str = Field(..., description="模型名称")
    display_name: str = Field(..., description="显示名称")
    business_process: str = Field(..., description="业务过程描述")
    grain: str = Field(..., description="粒度声明")
    fact_tables: list[dict] = Field(..., description="事实表设计")
    dimensions: list[dict] = Field(..., description="维度设计")
    conformed_dimensions: list[dict] | None = Field(None, description="一致性维度")
    model_type: str = Field("star", description="模型类型：star/snowflake/constellation")
    description: str | None = Field(None, description="描述")


class UpdateDimensionalModelRequest(BaseModel):
    display_name: str | None = None
    description: str | None = None
    business_process: str | None = None
    grain: str | None = None
    fact_tables: list[dict] | None = None
    dimensions: list[dict] | None = None
    conformed_dimensions: list[dict] | None = None
    model_type: str | None = None


@router.post("")
def create_dimensional_model(
    data: CreateDimensionalModelRequest, db: Session = Depends(get_db)
):
    """创建维度模型。"""
    try:
        model = dim_model_service.create_model(
            db,
            modeling_case_id=data.modeling_case_id,
            domain_id=data.domain_id,
            ontology_id=data.ontology_id,
            name=data.name,
            display_name=data.display_name,
            business_process=data.business_process,
            grain=data.grain,
            fact_tables=data.fact_tables,
            dimensions=data.dimensions,
            conformed_dimensions=data.conformed_dimensions,
            model_type=data.model_type,
            description=data.description,
        )
        return model
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{model_id}")
def get_dimensional_model(model_id: str, db: Session = Depends(get_db)):
    """获取维度模型详情。"""
    model = dim_model_service.get_model(db, model_id)
    if not model:
        raise HTTPException(status_code=404, detail="维度模型不存在")
    return model


@router.get("")
def list_dimensional_models(
    modeling_case_id: str | None = None,
    domain_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
    db: Session = Depends(get_db),
):
    """列出维度模型。"""
    models = dim_model_service.list_models(
        db,
        modeling_case_id=modeling_case_id,
        domain_id=domain_id,
        status=status,
        limit=limit,
    )
    return {"items": models, "total": len(models)}


@router.put("/{model_id}")
def update_dimensional_model(
    model_id: str,
    data: UpdateDimensionalModelRequest,
    db: Session = Depends(get_db),
):
    """更新维度模型。"""
    try:
        updates = {k: v for k, v in data.model_dump().items() if v is not None}
        model = dim_model_service.update_model(db, model_id, updates)
        return model
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{model_id}/validate")
def validate_dimensional_model(model_id: str, db: Session = Depends(get_db)):
    """验证维度模型。"""
    try:
        result = dim_model_service.validate_model(db, model_id)
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{model_id}/confirm")
def confirm_dimensional_model(model_id: str, db: Session = Depends(get_db)):
    """确认维度模型。"""
    try:
        model = dim_model_service.confirm_model(db, model_id)
        return model
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{model_id}/compile")
def compile_dimensional_model(model_id: str, db: Session = Depends(get_db)):
    """编译维度模型为物化契约。"""
    try:
        result = dim_model_service.compile_model(db, model_id)
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
