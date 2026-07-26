"""data app grafana model: tiles->panels, widget_id->panel_id, unify types

Revision ID: d8e9f0a1b2c3
Revises: c7d8e9f0a1b2
Create Date: 2026-07-26

G3（Grafana 范式收敛）：数据应用统一为 Dashboard + Panel。
- 看板 spec：字段 tiles → panels，面板内 widget_id → panel_id（向后兼容读取仍保留）。
- 旧 data_table 应用一次性转为 dashboard（按数据集生成表格面板）。
- screen（像素大屏）暂保留 app_type，仅在读取层作为看板的 canvas 布局兼容渲染。

纯数据迁移（不改表结构），在 SQLite / PostgreSQL 均可执行；对空库为 no-op。
"""

from __future__ import annotations

import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d8e9f0a1b2c3"
down_revision: Union[str, Sequence[str], None] = "c7d8e9f0a1b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _loads(value):
    if not value:
        return {}
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return {}


def _rename_panels(spec: dict) -> dict:
    """tiles → panels；面板内 widget_id → panel_id。"""
    panels = spec.get("panels")
    if panels is None:
        panels = spec.get("tiles") or []
    normalized = []
    for p in panels:
        if isinstance(p, dict):
            p = dict(p)
            if "widget_id" in p and "panel_id" not in p:
                p["panel_id"] = p.pop("widget_id")
            elif "widget_id" in p:
                p.pop("widget_id", None)
        normalized.append(p)
    spec["panels"] = normalized
    spec.pop("tiles", None)
    return spec


def _table_app_to_dashboard(spec: dict, dataset_ids: list[str]) -> dict:
    """旧 data_table spec → dashboard grid spec（每个数据集一个表格面板）。"""
    panels = []
    for idx, _ds in enumerate(dataset_ids):
        panels.append(
            {
                "id": f"t{idx + 1}",
                "widgetType": "table",
                "title": "表格",
                "datasetIndex": idx,
                "x": (idx % 2) * 6,
                "y": (idx // 2) * 8,
                "w": 6,
                "h": 8,
            }
        )
    return {
        "layout": "grid",
        "grid": {"cols": 12, "rowHeight": 40, "gap": 12},
        "theme": spec.get("theme") or {"preset": "light", "bg": "#f5f7fa"},
        "filters": spec.get("filters") or [],
        "panels": panels,
    }


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "data_apps" not in insp.get_table_names():
        return

    apps = list(
        bind.execute(
            sa.text("SELECT id, app_type, spec_json FROM data_apps")
        )
    )
    for app_id, app_type, spec_json in apps:
        spec = _loads(spec_json)
        new_type = app_type

        if app_type == "dashboard":
            spec = _rename_panels(spec)
        elif app_type == "data_table":
            ds_rows = list(
                bind.execute(
                    sa.text(
                        "SELECT id FROM data_app_datasets "
                        "WHERE app_id = :aid ORDER BY created_at"
                    ),
                    {"aid": app_id},
                )
            )
            dataset_ids = [r[0] for r in ds_rows]
            spec = _table_app_to_dashboard(spec, dataset_ids)
            new_type = "dashboard"
        elif app_type == "screen":
            # 像素大屏：保留 canvas（widgets），仅顺带清理可能存在的 tiles 字段
            if "tiles" in spec:
                spec = _rename_panels(spec)
        else:
            continue

        bind.execute(
            sa.text(
                "UPDATE data_apps SET app_type = :t, spec_json = :s WHERE id = :aid"
            ),
            {"t": new_type, "s": json.dumps(spec, ensure_ascii=False), "aid": app_id},
        )

    # 发布快照中的 spec_snapshot_json 同步改名（panels），保证已发布看板对外渲染一致
    if "data_app_versions" in insp.get_table_names():
        versions = list(
            bind.execute(
                sa.text("SELECT id, spec_snapshot_json FROM data_app_versions")
            )
        )
        for vid, snap_json in versions:
            snap = _loads(snap_json)
            if not snap or ("tiles" not in snap and "panels" not in snap):
                continue
            snap = _rename_panels(snap)
            bind.execute(
                sa.text(
                    "UPDATE data_app_versions SET spec_snapshot_json = :s WHERE id = :vid"
                ),
                {"s": json.dumps(snap, ensure_ascii=False), "vid": vid},
            )


def downgrade() -> None:
    # 兼容读取保留 tiles/widget_id，无需强制回滚数据；此处仅将 panels 还原为 tiles。
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "data_apps" not in insp.get_table_names():
        return
    apps = list(bind.execute(sa.text("SELECT id, spec_json FROM data_apps")))
    for app_id, spec_json in apps:
        spec = _loads(spec_json)
        if "panels" not in spec:
            continue
        panels = []
        for p in spec.get("panels") or []:
            if isinstance(p, dict) and "panel_id" in p:
                p = dict(p)
                p["widget_id"] = p.pop("panel_id")
            panels.append(p)
        spec["tiles"] = panels
        spec.pop("panels", None)
        bind.execute(
            sa.text("UPDATE data_apps SET spec_json = :s WHERE id = :aid"),
            {"s": json.dumps(spec, ensure_ascii=False), "aid": app_id},
        )
