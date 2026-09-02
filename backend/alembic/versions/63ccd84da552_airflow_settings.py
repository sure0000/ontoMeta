"""airflow_settings: 物化编排调度器配置（M10）

物化改由 Airflow 编排后，需要一处存「调度器怎么连、DAG 往哪投递」。
比照 datahub_settings / cube_settings 的单例配置行做法。
**目标库与源库的凭据不在这里**——那些是 Airflow 侧的 Connection，
本表只存 conn_id 之类的引用。

Revision ID: 63ccd84da552
Revises: c3d4e5f6a7b8
Create Date: 2026-08-02
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "63ccd84da552"
down_revision: Union[str, Sequence[str], None] = "c3d4e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "airflow_settings",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "endpoint", sa.String(length=512), nullable=False, server_default="http://localhost:8081"
        ),
        sa.Column("username", sa.String(length=255), nullable=True),
        sa.Column("password", sa.String(length=512), nullable=True),
        sa.Column("token", sa.String(length=512), nullable=True),
        sa.Column("api_version", sa.String(length=10), nullable=False, server_default="v1"),
        sa.Column("dags_dir", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("jobs_dir", sa.String(length=512), nullable=False, server_default=""),
        sa.Column(
            "warehouse_conn_id",
            sa.String(length=255),
            nullable=False,
            server_default="warehouse_default",
        ),
        sa.Column(
            "seatunnel_image",
            sa.String(length=255),
            nullable=False,
            server_default="apache/seatunnel:2.3.11",
        ),
        # 默认不启用：没有 Airflow 的环境保持既有 direct 直连行为不变。
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("airflow_settings")
