from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import settings_service
from app.database import get_db
from app.schemas import (
    DatahubSettingsOut,
    DatahubSettingsUpdate,
    DraftGenerationSettingsOut,
    DraftGenerationSettingsUpdate,
    LlmModelOption,
    LlmServiceConfigCreate,
    LlmServiceConfigDetail,
    LlmServiceConfigOut,
    LlmServiceConfigUpdate,
)
from app.services.settings_service import mask_secret

router = APIRouter()

def _llm_service_out(service) -> LlmServiceConfigOut:
    return LlmServiceConfigOut(
        id=service.id,
        name=service.name,
        provider=service.provider,
        api_base_url=service.api_base_url,
        model=service.model,
        is_default=service.is_default,
        enabled=service.enabled,
        use_mock=service.use_mock,
        api_key_set=bool(service.api_key),
        api_key_hint=mask_secret(service.api_key),
        created_at=service.created_at,
        updated_at=service.updated_at,
    )


def _llm_service_detail(service) -> LlmServiceConfigDetail:
    # 不再回显明文 API Key；列表/详情仅提供 hint，编辑时留空表示保持不变
    return LlmServiceConfigDetail(
        **_llm_service_out(service).model_dump(),
        api_key=None,
    )


def _datahub_settings_out(row) -> DatahubSettingsOut:
    return DatahubSettingsOut(
        gms_url=row.gms_url,
        frontend_url=row.frontend_url,
        token_set=bool(row.token),
        token_hint=mask_secret(row.token),
        use_mock=row.use_mock,
        updated_at=row.updated_at,
    )

@router.get("/config")
def get_app_config(db: Session = Depends(get_db)):
    datahub = settings_service.get_datahub_runtime(db)
    return {
        "datahub_gms_url": datahub.gms_url,
        "datahub_frontend_url": datahub.frontend_url,
    }


@router.get("/settings/llm-models", response_model=list[LlmModelOption])
def list_llm_models():
    return settings_service.list_llm_models()


@router.get("/settings/llm-services", response_model=list[LlmServiceConfigOut])
def list_llm_services(db: Session = Depends(get_db)):
    return [_llm_service_out(item) for item in settings_service.list_llm_services(db)]


@router.post("/settings/llm-services", response_model=LlmServiceConfigDetail)
def create_llm_service(data: LlmServiceConfigCreate, db: Session = Depends(get_db)):
    service = settings_service.create_llm_service(db, data.model_dump())
    return _llm_service_detail(service)


@router.get("/settings/llm-services/{service_id}", response_model=LlmServiceConfigDetail)
def get_llm_service(service_id: str, db: Session = Depends(get_db)):
    service = settings_service.get_llm_service(db, service_id)
    if not service:
        raise HTTPException(status_code=404, detail="LLM 服务配置不存在")
    return _llm_service_detail(service)


@router.put("/settings/llm-services/{service_id}", response_model=LlmServiceConfigDetail)
def update_llm_service(
    service_id: str, data: LlmServiceConfigUpdate, db: Session = Depends(get_db)
):
    payload = data.model_dump(exclude_unset=True)
    service = settings_service.update_llm_service(db, service_id, payload)
    if not service:
        raise HTTPException(status_code=404, detail="LLM 服务配置不存在")
    return _llm_service_detail(service)


@router.delete("/settings/llm-services/{service_id}")
def delete_llm_service(service_id: str, db: Session = Depends(get_db)):
    if not settings_service.delete_llm_service(db, service_id):
        raise HTTPException(status_code=404, detail="LLM 服务配置不存在")
    return {"id": service_id, "deleted": True}


@router.get("/settings/datahub", response_model=DatahubSettingsOut)
def get_datahub_settings(db: Session = Depends(get_db)):
    return _datahub_settings_out(settings_service.get_datahub_settings(db))


@router.put("/settings/datahub", response_model=DatahubSettingsOut)
def update_datahub_settings(data: DatahubSettingsUpdate, db: Session = Depends(get_db)):
    row = settings_service.update_datahub_settings(db, data.model_dump())
    return _datahub_settings_out(row)


@router.get("/settings/draft-generation", response_model=DraftGenerationSettingsOut)
def get_draft_generation_settings(db: Session = Depends(get_db)):
    return settings_service.get_draft_generation_settings(db)


@router.put("/settings/draft-generation", response_model=DraftGenerationSettingsOut)
def update_draft_generation_settings(
    data: DraftGenerationSettingsUpdate, db: Session = Depends(get_db)
):
    return settings_service.update_draft_generation_settings(db, data.model_dump())


