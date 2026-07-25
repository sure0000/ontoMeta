"""data app public share: token / password / expiry

Revision ID: b6c7d8e9f0a1
Revises: a5b6c7d8e9f0
Create Date: 2026-07-25

D3：看板公开分享（免登录只读链接 + 可选口令 + 有效期）。
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b6c7d8e9f0a1"
down_revision: Union[str, Sequence[str], None] = "a5b6c7d8e9f0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("data_apps")}
    if "public_token" not in cols:
        op.add_column("data_apps", sa.Column("public_token", sa.String(length=64), nullable=True))
        op.create_index("ix_data_apps_public_token", "data_apps", ["public_token"], unique=True)
    if "public_enabled" not in cols:
        op.add_column("data_apps", sa.Column("public_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))
    if "public_password_hash" not in cols:
        op.add_column("data_apps", sa.Column("public_password_hash", sa.String(length=128), nullable=True))
    if "public_expires_at" not in cols:
        op.add_column("data_apps", sa.Column("public_expires_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_index("ix_data_apps_public_token", table_name="data_apps")
    for col in ("public_expires_at", "public_password_hash", "public_enabled", "public_token"):
        op.drop_column("data_apps", col)
