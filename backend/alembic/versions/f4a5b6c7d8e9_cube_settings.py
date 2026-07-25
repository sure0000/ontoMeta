"""cube settings: db-backed cube config (managed in web settings, not env)

Revision ID: f4a5b6c7d8e9
Revises: e3f4a5b6c7d8
Create Date: 2026-07-25

把 Cube 外挂配置从环境变量/配置文件迁移到 DB，改为在设置页管理。
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f4a5b6c7d8e9"
down_revision: Union[str, Sequence[str], None] = "e3f4a5b6c7d8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "cube_settings" not in set(insp.get_table_names()):
        op.create_table(
            "cube_settings",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("api_url", sa.String(length=512), nullable=False, server_default="http://localhost:4000"),
            sa.Column("api_secret", sa.String(length=512), nullable=True),
            sa.Column("use_mock", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("preagg_refresh", sa.String(length=50), nullable=False, server_default="1 hour"),
            sa.Column("tenant_dimension", sa.String(length=100), nullable=True),
            sa.Column("timeout_seconds", sa.Integer(), nullable=False, server_default="30"),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
        )


def downgrade() -> None:
    op.drop_table("cube_settings")
