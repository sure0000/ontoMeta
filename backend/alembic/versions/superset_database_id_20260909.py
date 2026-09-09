"""superset database binding: add data_sources.superset_database_id

Revision ID: superset_database_id_20260909
Revises: drop_data_apps_20260908
Create Date: 2026-09-09

Superset 建数据集要指定挂在哪条 database（**连接**，不是库）上。此前这是设置页里的
一个全局 ``database_id``，等于假定全站落点都在同一个引擎实例上。落点实际归属哪个
数据源，本体侧一直记着（``OntologyWarehouseDeployment.doris_datasource_id`` /
``IngestionContract.doris_datasource_id``），所以改为按落点的数据源解析。

这一列是解析结果的缓存，也是自动匹配认不出来时的人工兜底（直接 PATCH 数据源即可）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "superset_database_id_20260909"
down_revision: str | Sequence[str] | None = "drop_data_apps_20260908"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 存在性守卫：测试库由 Base.metadata.create_all 建，模型改完这一列可能已经在了。
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("data_sources")}
    if "superset_database_id" not in cols:
        op.add_column(
            "data_sources",
            sa.Column("superset_database_id", sa.Integer(), nullable=True),
        )


def downgrade() -> None:
    op.drop_column("data_sources", "superset_database_id")
