"""data source starrocks multi-catalog: add data_sources.catalog_name

Revision ID: d4e5f6a7b8ca
Revises: d4e5f6a7b8c9
Create Date: 2026-08-10

统一查询网关（StarRocks 多目录）：为数据源增加 catalog_name 列。
- NULL/"internal" = warehouse（数仓投影，agent 默认查这里）
- 其他值（"erp"/"crm"/...）= 源系统在 StarRocks 里注册的 JDBC catalog 名，
  显式 target 参数才查（warehouse-first 语义）
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d4e5f6a7b8ca"
down_revision: Union[str, Sequence[str], None] = "d4e5f6a7b8c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("data_sources")}
    if "catalog_name" not in cols:
        op.add_column("data_sources", sa.Column("catalog_name", sa.String(length=100), nullable=True))


def downgrade() -> None:
    op.drop_column("data_sources", "catalog_name")
