"""搬运工具改为自动决策，设置页保留强制指定（空 = 自动）

物化弹窗原来逐次让人选 seatunnel/datax/flink。那个选择在默认的 runner 通道下不参与执行
（档位由 runner 逐表自选），却会以「没有可用镜像」的名义拦住提交。改为由
``services/sync_tool_resolver`` 统一决策，本列是唯一的人工覆盖入口。

空串 = 自动，与升级前的行为一致（原来不填 sync_tool 即用 DEFAULT_SYNC_TOOL，docker 通道
下自动决策的首选也是它），故已有部署无需数据搬运。

Revision ID: c6d7e8f9a0b1
Revises: b5c6d7e8f9a0
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c6d7e8f9a0b1"
down_revision = "b5c6d7e8f9a0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "airflow_settings",
        sa.Column("sync_tool", sa.String(32), nullable=False, server_default=sa.text("''")),
    )


def downgrade() -> None:
    op.drop_column("airflow_settings", "sync_tool")
