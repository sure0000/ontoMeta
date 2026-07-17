"""B9: version diff/snapshot columns + draft task error_summary

Revision ID: b9a1b2c3d4e5
Revises: b8e0a1c2d3f4
Create Date: 2026-07-17
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b9a1b2c3d4e5"
down_revision: Union[str, Sequence[str], None] = "b8e0a1c2d3f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("version_records", schema=None) as batch_op:
        batch_op.add_column(sa.Column("diff_json", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("snapshot_json", sa.Text(), nullable=True))

    with op.batch_alter_table("draft_generation_tasks", schema=None) as batch_op:
        batch_op.add_column(sa.Column("error_summary", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("draft_generation_tasks", schema=None) as batch_op:
        batch_op.drop_column("error_summary")

    with op.batch_alter_table("version_records", schema=None) as batch_op:
        batch_op.drop_column("snapshot_json")
        batch_op.drop_column("diff_json")
