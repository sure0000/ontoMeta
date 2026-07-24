"""draft_generation_settings: 可动态调整的分块并发度配置

Revision ID: f3a4b5c6d7e8
Revises: e2f3a4b5c6d7
Create Date: 2026-07-23
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f3a4b5c6d7e8"
down_revision: Union[str, Sequence[str], None] = "e2f3a4b5c6d7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "draft_generation_settings",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("object_chunk_concurrency", sa.Integer(), nullable=False, server_default="4"),
        sa.Column("relation_chunk_concurrency", sa.Integer(), nullable=False, server_default="4"),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("draft_generation_settings")
