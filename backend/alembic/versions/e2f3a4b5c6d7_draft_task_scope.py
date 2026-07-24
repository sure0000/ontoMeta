"""draft_generation_tasks: add scope column (full/objects/relations)

Revision ID: e2f3a4b5c6d7
Revises: d1e2f3a4b5c6
Create Date: 2026-07-23
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e2f3a4b5c6d7"
down_revision: Union[str, Sequence[str], None] = "d1e2f3a4b5c6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("draft_generation_tasks", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "scope",
                sa.String(length=20),
                nullable=False,
                server_default="full",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("draft_generation_tasks", schema=None) as batch_op:
        batch_op.drop_column("scope")
