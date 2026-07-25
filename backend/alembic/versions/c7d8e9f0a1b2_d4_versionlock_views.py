"""D4: version-lock widgets snapshot + dashboard view_count

Revision ID: c7d8e9f0a1b2
Revises: b6c7d8e9f0a1
Create Date: 2026-07-25

D4：发布时快照被引用图表定义（引用版本锁定）；看板访问计数。
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c7d8e9f0a1b2"
down_revision: Union[str, Sequence[str], None] = "b6c7d8e9f0a1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    app_cols = {c["name"] for c in insp.get_columns("data_apps")}
    if "view_count" not in app_cols:
        op.add_column("data_apps", sa.Column("view_count", sa.Integer(), nullable=False, server_default="0"))
    ver_cols = {c["name"] for c in insp.get_columns("data_app_versions")}
    if "widgets_snapshot_json" not in ver_cols:
        op.add_column("data_app_versions", sa.Column("widgets_snapshot_json", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("data_app_versions", "widgets_snapshot_json")
    op.drop_column("data_apps", "view_count")
