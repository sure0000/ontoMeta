"""数据源与 Doris 数仓配置的管理 API。

图表与看板已改由 Apache Superset 承载（见 ``api/superset.py``）：ontoMeta 不再自建
数据应用，这里只剩「连哪些库、默认数仓是哪个」这一层——它是物化、搬运、取数共用的地基。

均挂在 /api 前缀下，需 ONTOMETA_ADMIN_TOKEN（与其它管理路由一致）。
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import datasource_service
from app.database import get_db
from app.schemas import (
    DataSourceCreate,
    DataSourceOut,
    DataSourceUpdate,
    DorisWarehouseConfigOut,
    DorisWarehouseConfigUpdate,
)

router = APIRouter()


# --------------------------------------------------------------- data sources


@router.get("/data-sources", response_model=list[DataSourceOut])
def list_data_sources(db: Session = Depends(get_db)):
    return [datasource_service.serialize_data_source(d) for d in datasource_service.list_data_sources(db)]


@router.post("/data-sources", response_model=DataSourceOut)
def create_data_source(data: DataSourceCreate, db: Session = Depends(get_db)):
    try:
        ds = datasource_service.create_data_source(
            db,
            name=data.name,
            kind=data.kind,
            dsn_secret_ref=data.dsn_secret_ref,
            mapping=data.mapping,
            catalog_name=data.catalog_name,
            purpose=data.purpose,
            is_default_warehouse=data.is_default_warehouse,
            enabled=data.enabled,
        )
        return datasource_service.serialize_data_source(ds)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/data-sources/{ds_id}", response_model=DataSourceOut)
def update_data_source(
    ds_id: str, data: DataSourceUpdate, db: Session = Depends(get_db)
):
    try:
        ds = datasource_service.update_data_source(
            db, ds_id, **data.model_dump(exclude_unset=True)
        )
        return datasource_service.serialize_data_source(ds)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

@router.delete("/data-sources/{ds_id}")
def delete_data_source(ds_id: str, db: Session = Depends(get_db)):
    try:
        datasource_service.delete_data_source(db, ds_id)
        return {"status": "ok"}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/data-sources/{ds_id}/test", response_model=DataSourceOut)
def test_data_source(ds_id: str, db: Session = Depends(get_db)):
    try:
        return datasource_service.serialize_data_source(
            datasource_service.test_data_source(db, ds_id)
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/data-sources/{ds_id}/databases")
def list_data_source_databases(ds_id: str, db: Session = Depends(get_db)):
    """目标源上的库列表，供物化弹窗选落库位置。"""
    try:
        return {"databases": datasource_service.list_databases(db, ds_id)}
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
        return {"tables": datasource_service.list_tables(db, ds_id, database)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/data-sources/sync-catalogs")
def sync_data_source_catalogs(db: Session = Depends(get_db)):
    """StarRocks 多目录：把 catalog_name 非空的源库同步为 FE 上的外部 JDBC catalog。

    幂等（已存在跳过）；仓库源（kind=starrocks/doris）的连接串即 FE 端点。
    """
    from app.services.catalog_sync import sync_all_catalogs

    return sync_all_catalogs(db)


@router.get("/doris-warehouse", response_model=DorisWarehouseConfigOut | None)
def get_doris_warehouse_config(db: Session = Depends(get_db)):
    config = datasource_service.get_doris_config(db)
    return datasource_service.serialize_doris_config(config) if config else None


@router.put("/doris-warehouse", response_model=DorisWarehouseConfigOut)
def update_doris_warehouse_config(
    data: DorisWarehouseConfigUpdate, db: Session = Depends(get_db)
):
    try:
        config = datasource_service.save_doris_config(
            db, data.model_dump()
        )
        return datasource_service.serialize_doris_config(config)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
