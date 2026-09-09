"""Superset 资产的管理 REST（供前端「数据应用」页用）。

前端不直接说 MCP 协议，故把「列资产 / 对账 / 解除登记」用普通 REST 暴露，
走主后端的 AdminAuthMiddleware。

图表与看板一律走**跳转链接**在 Superset 里打开，ontoMeta 不做内嵌、不签 guest token。
数据逻辑一律走 ``app.services.superset_service``（与 MCP 工具共用同一份），REST 只是薄壳。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.auth import require_role
from app.database import get_db
from app.services import superset_service as svc

router = APIRouter()


@router.get("/superset/status")
def superset_status(db: Session = Depends(get_db)):
    """组件配没配好、对外地址是什么。**不返回任何凭据。**

    页面靠它区分「还没接 Superset」与「接了但这个域还没建过图」——
    这两种空状态该说的话完全不同。
    """
    try:
        cfg = svc.runtime(db)
    except svc.SupersetNotConfigured as exc:
        return {"configured": False, "reason": str(exc), "base_url": None}
    return {
        "configured": True,
        "reason": None,
        "base_url": cfg.public_base_url,
    }


@router.get("/superset/assets")
def list_superset_assets(
    asset_type: str | None = Query(None),
    ontology_id: str | None = Query(None),
    q: str | None = Query(None),
    limit: int = Query(100, ge=1, le=200),
    db: Session = Depends(get_db),
):
    try:
        cfg = svc.runtime(db)
    except svc.SupersetNotConfigured:
        # 没配 Superset 不等于出错：页面要能显示「还没接」而不是一个红色报错。
        cfg = None
    rows = svc.list_assets(
        db, asset_type=asset_type, ontology_id=ontology_id, keyword=q, limit=limit
    )
    return {"items": svc.serialize_assets(db, rows, cfg)}


@router.post("/superset/assets/reconcile", dependencies=[Depends(require_role("editor"))])
def reconcile_superset_assets(db: Session = Depends(get_db)):
    """与 Superset 对一次账。只改 ``state``，不删登记、也不动 Superset。"""
    try:
        cfg = svc.runtime(db)
        with svc.client(cfg) as sc:
            counts = svc.reconcile(db, cfg, sc)
    except svc.SupersetNotConfigured as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Superset 调用失败：{exc}") from exc
    return counts


@router.delete(
    "/superset/assets/{asset_id}", dependencies=[Depends(require_role("editor"))]
)
def unlink_superset_asset(asset_id: str, db: Session = Depends(get_db)):
    """只解除登记，**不删 Superset 里的图或看板**。

    删外部系统里的东西是另一个决定，得到 Superset 那边做——在这里顺手删掉，
    平台上只表现为"从列表消失了"，别人正在看的看板却没了。
    """
    if not svc.unlink(db, asset_id):
        raise HTTPException(status_code=404, detail="资产不存在")
    return {"ok": True}
