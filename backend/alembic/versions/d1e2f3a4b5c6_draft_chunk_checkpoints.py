"""draft_chunk_checkpoints: 草稿分块生成的按块检查点

Revision ID: d1e2f3a4b5c6
Revises: c0d1e2f3a4b5
Create Date: 2026-07-21
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d1e2f3a4b5c6"
down_revision: Union[str, Sequence[str], None] = "c0d1e2f3a4b5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "draft_chunk_checkpoints",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("domain_context_id", sa.String(length=36), nullable=False),
        sa.Column("chunk_key", sa.String(length=64), nullable=False),
        sa.Column("output_json", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["domain_context_id"], ["domain_contexts.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "domain_context_id", "chunk_key", name="uq_draft_chunk_checkpoint"
        ),
    )
    with op.batch_alter_table("draft_chunk_checkpoints", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_draft_chunk_checkpoints_domain_context_id"),
            ["domain_context_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_draft_chunk_checkpoints_chunk_key"),
            ["chunk_key"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("draft_chunk_checkpoints", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_draft_chunk_checkpoints_chunk_key"))
        batch_op.drop_index(
            batch_op.f("ix_draft_chunk_checkpoints_domain_context_id")
        )
    op.drop_table("draft_chunk_checkpoints")
