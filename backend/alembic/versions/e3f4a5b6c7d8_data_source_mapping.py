"""data source physical mapping: add data_sources.mapping_json

Revision ID: e3f4a5b6c7d8
Revises: d2e3f4a5b6c7
Create Date: 2026-07-25

阶段 2：为数据源增加物理映射（本体 name → 物理表/列名）列。
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e3f4a5b6c7d8"
down_revision: Union[str, Sequence[str], None] = "d2e3f4a5b6c7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("data_sources")}
    if "mapping_json" not in cols:
        op.add_column("data_sources", sa.Column("mapping_json", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("data_sources", "mapping_json")
