"""object_types: add role_signals classification-evidence column

保存分类器算出的结构化判定证据（JSON 文本）：score / needs_review /
signals（主键结构、外键入度、字段语义占比、tech_score、连通性等），供复核
界面展示「判定依据」。机器每次生成重算并直接覆盖，非用户可编辑，不参与三方合并。

Revision ID: f0a1b2c3d4e5
Revises: e9f0a1b2c3d4
Create Date: 2026-07-28
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f0a1b2c3d4e5"
down_revision: Union[str, Sequence[str], None] = "e9f0a1b2c3d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("object_types", schema=None) as batch_op:
        batch_op.add_column(sa.Column("role_signals", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("object_types", schema=None) as batch_op:
        batch_op.drop_column("role_signals")
