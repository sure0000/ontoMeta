"""dependency_components: 依赖组件统一部署管理注册表（Phase 0）

见 docs/DEPENDENCY_DEPLOYMENT_REDESIGN.md §3。本表与既有五张设置表并行存在，
Phase 0 不接读取侧；Phase 1 起读取侧改为从本表投影。
ERPNext 等外部源库不在此纳管（走 data_sources）。

Revision ID: 9a2b3c4d5e6f
Revises: 821bc8acf9c4
Create Date: 2026-08-07
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "9a2b3c4d5e6f"
down_revision: Union[str, Sequence[str], None] = "821bc8acf9c4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "dependency_components",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("key", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("deploy_mode", sa.String(length=16), nullable=False, server_default="external"),
        sa.Column("deploy_spec_json", sa.Text(), nullable=True),
        sa.Column("deploy_status", sa.String(length=16), nullable=False, server_default="not_deployed"),
        sa.Column("deploy_error", sa.Text(), nullable=True),
        sa.Column("connection_json", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
    )
    op.create_index("ix_dependency_components_key", "dependency_components", ["key"])


def downgrade() -> None:
    op.drop_index("ix_dependency_components_key", table_name="dependency_components")
    op.drop_table("dependency_components")
