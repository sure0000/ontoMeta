"""dependency_components.deploy_log: 部署日志（逐命令），部署失败时前端可查看定位。

Revision ID: d4e5f6a7b8c9
Revises: 9a2b3c4d5e6f
Create Date: 2026-08-08
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import context, op

revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, Sequence[str], None] = "9a2b3c4d5e6f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = (
        set()
        if context.is_offline_mode()
        else {
            column["name"]
            for column in sa.inspect(bind).get_columns("dependency_components")
        }
    )
    if "deploy_log" not in columns:
        op.add_column(
            "dependency_components",
            sa.Column("deploy_log", sa.Text(), nullable=True),
        )


def downgrade() -> None:
    op.drop_column("dependency_components", "deploy_log")
