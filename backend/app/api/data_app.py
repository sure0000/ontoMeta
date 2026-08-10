"""数据应用（Data App）管理 API：数据源、应用 CRUD、编译、预览、发布、对话生成。

均挂在 /api 前缀下，需 ONTOMETA_ADMIN_TOKEN（与其它管理路由一致）。
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import data_app_service
from app.database import get_db
from app.schemas import (
    AddWidgetToDashboardRequest,
    DataAppCompileResult,
    DataAppCreate,
    DataAppDetail,
    DataAppPreviewRequest,
    DataAppPreviewResult,
    DataAppPublishRequest,
    DataAppSummary,
    DataAppUpdate,
    DataAppVersionOut,
    DataAppWidgetCreate,
    DataAppWidgetOut,
    DataAppWidgetUpdate,
    DataSourceCreate,
    DataSourceOut,
    DataSourceUpdate,
    GenerateAppFromChatRequest,
    GenerateWidgetFromChatRequest,
    PublicShareRequest,
    PublicShareStatus,
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
        catalog_name=data.catalog_name,
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


@router.get("/data-sources/{ds_id}/databases")
def list_data_source_databases(ds_id: str, db: Session = Depends(get_db)):
    """目标源上的库列表，供物化弹窗选落库位置。"""
    try:
        return {"databases": data_app_service.list_databases(db, ds_id)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/data-sources/{ds_id}/tables")
def list_data_source_tables(
    ds_id: str,
    database: str | None = Query(None, description="库名；缺省用连接串里的默认库"),
    db: Session = Depends(get_db),
):
    """某个库下已有的表，供物化弹窗推荐表名并提示「已存在」。"""
    try:
        return {"tables": data_app_service.list_tables(db, ds_id, database)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/data-sources/sync-catalogs")
def sync_data_source_catalogs(db: Session = Depends(get_db)):
    """StarRocks 多目录：把 catalog_name 非空的源库同步为 FE 上的外部 JDBC catalog。

    幂等（已存在跳过）；仓库源（kind=starrocks/doris）的连接串即 FE 端点。
    """
    from app.services.catalog_sync import sync_all_catalogs

    return sync_all_catalogs(db)


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


@router.get("/data-apps/{app_id}/lineage")
def get_data_app_lineage(app_id: str, db: Session = Depends(get_db)):
    """看板血缘：看板 → 图表/数据集 → 本体对象/字段。"""
    try:
        return data_app_service.lineage(db, app_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# --------------------------------------------------------------- public share


@router.get("/data-apps/{app_id}/share", response_model=PublicShareStatus)
def get_share_status(app_id: str, db: Session = Depends(get_db)):
    app = data_app_service.get_app(db, app_id)
    if not app:
        raise HTTPException(status_code=404, detail="数据应用不存在")
    return data_app_service.public_share_status(app)


@router.post("/data-apps/{app_id}/share", response_model=PublicShareStatus)
def enable_share(app_id: str, data: PublicShareRequest, db: Session = Depends(get_db)):
    try:
        app = data_app_service.enable_public_share(
            db, app_id, password=data.password, expires_in_days=data.expires_in_days
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return data_app_service.public_share_status(app)


@router.delete("/data-apps/{app_id}/share", response_model=PublicShareStatus)
def disable_share(app_id: str, db: Session = Depends(get_db)):
    try:
        app = data_app_service.disable_public_share(db, app_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return data_app_service.public_share_status(app)


# ------------------------------------------------------------- cube model gen


@router.get("/ontologies/{ontology_id}/cube-model")
def get_ontology_cube_model(ontology_id: str, db: Session = Depends(get_db)):
    """为已发布本体生成 Cube data model（含预聚合/refreshKey/joins）。"""
    return data_app_service.generate_cube_model(db, ontology_id)


@router.get("/ontologies/{ontology_id}/cube-model/files")
def get_ontology_cube_model_files(ontology_id: str, db: Session = Depends(get_db)):
    """生成可直接部署的 Cube 文件（model/cubes/*.js + cube.js，含 RLS queryRewrite）。

    运维把返回的每个文件落盘到 Cube 的挂载目录（./cube）即可。
    """
    return {"files": data_app_service.generate_cube_model_files(db, ontology_id)}


# ----------------------------------------------------------------- widgets


@router.get("/data-app-widgets", response_model=list[DataAppWidgetOut])
def list_widgets(
    domain_id: str | None = Query(None),
    q: str | None = Query(None),
    widget_type: str | None = Query(None),
    db: Session = Depends(get_db),
):
    return [
        data_app_service.serialize_widget(w)
        for w in data_app_service.list_widgets(
            db, domain_id=domain_id, q=q, widget_type=widget_type
        )
    ]


@router.post("/data-app-widgets", response_model=DataAppWidgetOut)
def create_widget(data: DataAppWidgetCreate, db: Session = Depends(get_db)):
    try:
        w = data_app_service.create_widget(
            db,
            domain_id=data.domain_id,
            name=data.name,
            description=data.description,
            widget_type=data.widget_type,
            primary_object_type_id=data.primary_object_type_id,
            binding=data.binding.model_dump(),
            viz=data.viz,
            data_source_id=data.data_source_id,
            source=data.source,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return data_app_service.serialize_widget(w)


@router.get("/data-app-widgets/{widget_id}", response_model=DataAppWidgetOut)
def get_widget(widget_id: str, db: Session = Depends(get_db)):
    w = data_app_service.get_widget(db, widget_id)
    if not w:
        raise HTTPException(status_code=404, detail="图表不存在")
    return data_app_service.serialize_widget(w)


@router.patch("/data-app-widgets/{widget_id}", response_model=DataAppWidgetOut)
def update_widget(widget_id: str, data: DataAppWidgetUpdate, db: Session = Depends(get_db)):
    payload = data.model_dump(exclude_unset=True)
    if "binding" in payload and data.binding is not None:
        payload["binding"] = data.binding.model_dump()
    try:
        w = data_app_service.update_widget(db, widget_id, **payload)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return data_app_service.serialize_widget(w)


@router.delete("/data-app-widgets/{widget_id}")
def delete_widget(widget_id: str, db: Session = Depends(get_db)):
    try:
        data_app_service.delete_widget(db, widget_id)
        return {"status": "ok"}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/data-app-widgets/{widget_id}/preview", response_model=DataAppPreviewResult)
def preview_widget(
    widget_id: str,
    body: DataAppPreviewRequest | None = None,
    limit: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
):
    runtime_filters = [f.model_dump() for f in body.runtime_filters] if body else None
    effective_limit = body.limit if body and body.limit else limit
    try:
        return data_app_service.preview_widget(
            db, widget_id, limit=effective_limit, runtime_filters=runtime_filters
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/data-apps/{app_id}/widgets", response_model=DataAppDetail)
def add_widget_to_dashboard(
    app_id: str, data: AddWidgetToDashboardRequest, db: Session = Depends(get_db)
):
    try:
        app = data_app_service.add_widget_to_dashboard(db, app_id, data.widget_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return data_app_service.serialize_app(db, app, detail=True)


# ------------------------------------------------------- chat bi → generate app


@router.post("/chat-bi/generate-widget", response_model=DataAppWidgetOut)
async def generate_widget_from_chat(
    data: GenerateWidgetFromChatRequest, db: Session = Depends(get_db)
):
    try:
        w = await data_app_service.generate_widget_from_chat(
            db,
            domain_id=data.domain_id,
            question=data.question,
            widget_type=data.widget_type,
            name=data.name,
            caliber_decomposition=data.caliber_decomposition,
            referenced_objects=data.referenced_objects,
            dashboard_id=data.dashboard_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return data_app_service.serialize_widget(w)


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
            caliber_decomposition=data.caliber_decomposition,
            referenced_objects=data.referenced_objects,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return data_app_service.serialize_app(db, app, detail=True)
