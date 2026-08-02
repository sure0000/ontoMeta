"""airflow_settings 瘦身：设置页只留连接信息，其余交给物化弹窗/工具/数据源

物化的搬运工具（seatunnel/datax/flink）与同步策略改由物化弹窗逐次选；目标仓的
Airflow Connection id 由目标数据源推导；DAG/作业投递目录属部署基础设施，由 config
环境变量给默认。故 airflow_settings 去掉 dags_dir / jobs_dir / warehouse_conn_id /
seatunnel_image 四列，只留 endpoint / 鉴权 / api_version / enabled。

Revision ID: d9e0f1a2b3c4
Revises: c8d9e0f1a2b3
Create Date: 2026-08-02
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d9e0f1a2b3c4"
down_revision: Union[str, Sequence[str], None] = "c8d9e0f1a2b3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_DROPPED = ("dags_dir", "jobs_dir", "warehouse_conn_id", "seatunnel_image")


def upgrade() -> None:
    with op.batch_alter_table("airflow_settings") as batch:
        for col in _DROPPED:
            batch.drop_column(col)


def downgrade() -> None:
    with op.batch_alter_table("airflow_settings") as batch:
        batch.add_column(
            sa.Column("dags_dir", sa.String(length=512), nullable=False, server_default="")
        )
        batch.add_column(
            sa.Column("jobs_dir", sa.String(length=512), nullable=False, server_default="")
        )
        batch.add_column(
            sa.Column(
                "warehouse_conn_id",
                sa.String(length=255),
                nullable=False,
                server_default="warehouse_default",
            )
        )
        batch.add_column(
            sa.Column(
                "seatunnel_image",
                sa.String(length=255),
                nullable=False,
                server_default="apache/seatunnel:2.3.11",
            )
        )
