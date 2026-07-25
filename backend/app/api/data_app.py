"""数据应用（Data App）管理 API：数据源、应用 CRUD、编译、预览、发布、对话生成。

均挂在 /api 前缀下，需 ONTOMETA_ADMIN_TOKEN（与其它管理路由一致）。
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import data_app_service
from app.database import get_db
from app.schemas import (
    DataAppCompileResult,
    DataAppCreate,
    DataAppDetail,
    DataAppPreviewRequest,
    DataAppPreviewResult,
    DataAppPublishRequest,
    DataAppSummary,
    DataAppUpdate,
    DataAppVersionOut,
    DataSourceCreate,
    DataSourceOut,
    DataSourceUpdate,
    GenerateAppFromChatRequest,
)

router = APIRouter()


# --------------------------------------------------------------- data sources


@router.get("/data-sources", response_model=list[DataSourceOut])
def list_data_sources(db: Session = Depends(get_db)):
    return [data_app_service.serialize_data_source(d) for d in data_app_service.list_data_sources(db)]


@router.post("/data-sources", response_model=DataSourceOut)
def create_data_source(data: DataSourceCreate, db: Session = Depends(get_db)):
    ds = data_app_service.create_data_source(
        db,
        name=data.name,
        kind=data.kind,
        dsn_secret_ref=data.dsn_secret_ref,
        mapping=data.mapping,
    )
    return data_app_service.serialize_data_source(ds)


@router.patch("/data-sources/{ds_id}", response_model=DataSourceOut)
def update_data_source(
    ds_id: str, data: DataSourceUpdate, db: Session = Depends(get_db)
):
    try:
        ds = data_app_service.update_data_source(
            db, ds_id, **data.model_dump(exclude_unset=True)
        )
        return data_app_service.serialize_data_source(ds)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/data-sources/{ds_id}")
def delete_data_source(ds_id: str, db: Session = Depends(get_db)):
    try:
        data_app_service.delete_data_source(db, ds_id)
        return {"status": "ok"}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/data-sources/{ds_id}/test", response_model=DataSourceOut)
def test_data_source(ds_id: str, db: Session = Depends(get_db)):
    try:
        return data_app_service.serialize_data_source(
            data_app_service.test_data_source(db, ds_id)
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ------------------------------------------------------------------ data apps


@router.get("/data-apps", response_model=list[DataAppSummary])
def list_data_apps(
    domain_id: str | None = Query(None),
    app_type: str | None = Query(None),
    db: Session = Depends(get_db),
):
    apps = data_app_service.list_apps(db, domain_id=domain_id, app_type=app_type)
    return [data_app_service.serialize_app(db, a) for a in apps]


@router.post("/data-apps", response_model=DataAppDetail)
def create_data_app(data: DataAppCreate, db: Session = Depends(get_db)):
    try:
        app = data_app_service.create_app(
            db,
            domain_id=data.domain_id,
            app_type=data.app_type,
            name=data.name,
            description=data.description,
            source=data.source,
            spec=data.spec,
            datasets=[d.model_dump() for d in data.datasets],
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return data_app_service.serialize_app(db, app, detail=True)


@router.get("/data-apps/{app_id}", response_model=DataAppDetail)
def get_data_app(app_id: str, db: Session = Depends(get_db)):
    app = data_app_service.get_app(db, app_id)
    if not app:
        raise HTTPException(status_code=404, detail="数据应用不存在")
    return data_app_service.serialize_app(db, app, detail=True)


@router.patch("/data-apps/{app_id}", response_model=DataAppDetail)
def update_data_app(app_id: str, data: DataAppUpdate, db: Session = Depends(get_db)):
    try:
        payload = data.model_dump(exclude_unset=True)
        datasets = payload.get("datasets")
        app = data_app_service.update_app(
            db,
            app_id,
            name=payload.get("name"),
            description=payload.get("description"),
            spec=payload.get("spec"),
            datasets=datasets,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return data_app_service.serialize_app(db, app, detail=True)


@router.delete("/data-apps/{app_id}")
def delete_data_app(app_id: str, db: Session = Depends(get_db)):
    try:
        data_app_service.delete_app(db, app_id)
        return {"status": "ok"}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ---------------------------------------------------------- compile / preview


@router.post(
    "/data-apps/{app_id}/datasets/{dataset_id}/compile",
    response_model=DataAppCompileResult,
)
def compile_dataset(app_id: str, dataset_id: str, db: Session = Depends(get_db)):
    try:
        return data_app_service.compile_dataset(db, app_id, dataset_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/data-apps/{app_id}/datasets/{dataset_id}/preview",
    response_model=DataAppPreviewResult,
)
def preview_dataset(
    app_id: str,
    dataset_id: str,
    body: DataAppPreviewRequest | None = None,
    limit: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
):
    runtime_filters = (
        [f.model_dump() for f in body.runtime_filters] if body else None
    )
    effective_limit = body.limit if body and body.limit else limit
    try:
        return data_app_service.preview_dataset(
            db,
            app_id,
            dataset_id,
            limit=effective_limit,
            runtime_filters=runtime_filters,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ------------------------------------------------------------------- publish


@router.post("/data-apps/{app_id}/publish", response_model=DataAppDetail)
def publish_data_app(
    app_id: str, data: DataAppPublishRequest, db: Session = Depends(get_db)
):
    try:
        app = data_app_service.publish_app(
            db,
            app_id,
            version_comment=data.version_comment,
            operator=data.operator,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return data_app_service.serialize_app(db, app, detail=True)


@router.get("/data-apps/{app_id}/versions", response_model=list[DataAppVersionOut])
def list_data_app_versions(app_id: str, db: Session = Depends(get_db)):
    return data_app_service.list_versions(db, app_id)


# ------------------------------------------------------------- cube model gen


@router.get("/ontologies/{ontology_id}/cube-model")
def get_ontology_cube_model(ontology_id: str, db: Session = Depends(get_db)):
    """为已发布本体生成 Cube data model（供运维部署到 Cube 服务）。"""
    model = data_app_service.generate_cube_model(db, ontology_id)
    return model


# ------------------------------------------------------- chat bi → generate app


@router.post("/chat-bi/generate-app", response_model=DataAppDetail)
async def generate_app_from_chat(
    data: GenerateAppFromChatRequest, db: Session = Depends(get_db)
):
    try:
        app = await data_app_service.generate_from_chat(
            db,
            domain_id=data.domain_id,
            app_type=data.app_type,
            question=data.question,
            conversation_id=data.conversation_id,
            name=data.name,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return data_app_service.serialize_app(db, app, detail=True)
