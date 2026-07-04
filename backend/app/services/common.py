"""服务层共享工具。"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import EntityChangeLog


def log_change(
    db: Session,
    entity_type: str,
    entity_id: str,
    action: str,
    operator: str | None = None,
    summary: str | None = None,
) -> None:
    """写入一条实体变更审计日志。仅 db.add，调用方负责 commit。"""
    db.add(
        EntityChangeLog(
            entity_type=entity_type,
            entity_id=entity_id,
            action=action,
            operator=operator,
            change_summary=summary,
        )
    )
