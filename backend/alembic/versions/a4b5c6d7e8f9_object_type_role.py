"""object_types: add table_role classification columns

标注对象是「业务对象」还是「普通数据表」：预生成阶段由结构/内容/拓扑信号
（主键结构、外键入度、字段语义画像）判定，role_reason 可追溯供人工确认。

Revision ID: a4b5c6d7e8f9
Revises: f3a4b5c6d7e8
Create Date: 2026-07-24
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a4b5c6d7e8f9"
down_revision: Union[str, Sequence[str], None] = "f3a4b5c6d7e8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("object_types", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "table_role",
                sa.String(length=50),
                nullable=False,
                server_default="business_object",
            )
        )
        batch_op.add_column(sa.Column("role_confidence", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("role_reason", sa.Text(), nullable=True))
        batch_op.create_index(
            "ix_object_types_table_role", ["table_role"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("object_types", schema=None) as batch_op:
        batch_op.drop_index("ix_object_types_table_role")
        batch_op.drop_column("role_reason")
        batch_op.drop_column("role_confidence")
        batch_op.drop_column("table_role")
