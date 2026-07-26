"""data app: screen -> dashboard(canvas), widgets -> panels

Revision ID: e9f0a1b2c3d4
Revises: d8e9f0a1b2c3
Create Date: 2026-07-26

G3.5（彻底消除第三种类型）：像素大屏 screen 并入 Dashboard 的 canvas 布局模式。
- app_type: screen → dashboard
- spec.layout: screen → canvas
- spec.widgets（含 rect）→ spec.panels（widgetType/rect/datasetIndex/panel_id）
- 发布快照 spec_snapshot_json 同步转换

纯数据迁移，SQLite / PostgreSQL 通用，空库 no-op。
"""

from __future__ import annotations

import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e9f0a1b2c3d4"
down_revision: Union[str, Sequence[str], None] = "d8e9f0a1b2c3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _loads(value):
    if not value:
        return {}
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return {}


def _screen_to_canvas(spec: dict) -> dict:
    widgets = spec.get("widgets") or []
    panels = []
    for i, w in enumerate(widgets):
        if not isinstance(w, dict):
            continue
        panel = {
            "id": w.get("id") or f"t{i + 1}",
            "widgetType": w.get("type") or "table",
            "title": w.get("title"),
            "datasetIndex": w.get("datasetIndex", 0),
            "rect": w.get("rect")
            or {"x": 40 + (i % 2) * 680, "y": 40 + (i // 2) * 440, "w": 640, "h": 360},
        }
        if w.get("widget_id"):
            panel["panel_id"] = w["widget_id"]
        panels.append(panel)
    return {
        "layout": "canvas",
        "canvas": spec.get("canvas") or {"width": 1920, "height": 1080, "bg": "#0b1a2e"},
        "theme": spec.get("theme") or {"preset": "dark", "bg": "#0b1a2e"},
        "filters": spec.get("filters") or [],
        "params": spec.get("params") or [],
        "panels": panels,
    }


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "data_apps" not in insp.get_table_names():
        return

    apps = list(
        bind.execute(
            sa.text("SELECT id, app_type, spec_json FROM data_apps WHERE app_type = 'screen'")
        )
    )
    for app_id, _app_type, spec_json in apps:
        spec = _screen_to_canvas(_loads(spec_json))
        bind.execute(
            sa.text(
                "UPDATE data_apps SET app_type = 'dashboard', spec_json = :s WHERE id = :aid"
            ),
            {"s": json.dumps(spec, ensure_ascii=False), "aid": app_id},
        )

    # 发布快照同步转换（screen 版式）
    if "data_app_versions" in insp.get_table_names():
        versions = list(
            bind.execute(
                sa.text("SELECT id, spec_snapshot_json FROM data_app_versions")
            )
        )
        for vid, snap_json in versions:
            snap = _loads(snap_json)
            if snap.get("layout") != "screen" and "widgets" not in snap:
                continue
            snap = _screen_to_canvas(snap)
            bind.execute(
                sa.text(
                    "UPDATE data_app_versions SET spec_snapshot_json = :s WHERE id = :vid"
                ),
                {"s": json.dumps(snap, ensure_ascii=False), "vid": vid},
            )


def downgrade() -> None:
    # 不做破坏性回滚：canvas 布局在读取层兼容，无需还原 app_type。
    pass
