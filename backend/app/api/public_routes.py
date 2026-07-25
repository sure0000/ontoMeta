"""公开分享：免登录只读看板/应用。路径 /api/public/*（已在 AdminAuthMiddleware 豁免）。

安全：仅 public_enabled 且未过期的应用可访问；可选访问口令。只读、无写操作。
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import data_app_service
from app.database import get_db

router = APIRouter()


@router.get("/public/data-apps/{token}")
def public_get_data_app(
    token: str,
    password: str | None = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    try:
        app = data_app_service.get_public_app(db, token, password=password)
    except ValueError as exc:
        msg = str(exc)
        if msg == "__PASSWORD_REQUIRED__":
            raise HTTPException(status_code=401, detail="需要访问口令") from exc
        if msg == "访问口令错误":
            raise HTTPException(status_code=403, detail=msg) from exc
        raise HTTPException(status_code=404, detail=msg) from exc

    render = data_app_service._render_app_data(db, app, limit=limit)
    data_app_service.record_view(db, app)
    return {
        "id": app.id,
        "name": app.name,
        "app_type": app.app_type,
        "description": app.description,
        "published_version": app.published_version,
        "spec": data_app_service.serialize_public_app(app, detail=True).get("spec"),
        "datasets": [{"id": d.id, "name": d.name} for d in app.datasets],
        "render": render,
    }
