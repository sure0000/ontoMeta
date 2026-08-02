"""datahub_settings.fabric: 物化目标表 URN 的环境标（M11 血缘）

构造目标 dataset URN 需要 DataHub 环境标（PROD/DEV/…）。源表 URN 自带 fabric
（来自 source_ref），这里只决定物化目标侧。默认 PROD——与本仓既有 URN 一致；
可在设置页调整，避免深层硬编。

Revision ID: b7c8d9e0f1a2
Revises: 63ccd84da552
Create Date: 2026-08-02
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b7c8d9e0f1a2"
down_revision: Union[str, Sequence[str], None] = "63ccd84da552"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "datahub_settings",
        sa.Column(
            "fabric", sa.String(length=20), nullable=False, server_default="PROD"
        ),
    )


def downgrade() -> None:
    op.drop_column("datahub_settings", "fabric")
