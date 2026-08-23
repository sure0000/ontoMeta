"""Add bounded incremental watermark and idempotency policy.

Revision ID: d1f2a3b4c5d6
Revises: d0e1f2a3b4c5
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d1f2a3b4c5d6"
down_revision: Union[str, Sequence[str], None] = "d0e1f2a3b4c5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("ingestion_contracts")}
    if "initial_watermark" not in columns:
        op.add_column("ingestion_contracts", sa.Column("initial_watermark", sa.String(255), nullable=True))
    if "late_arrival_policy" not in columns:
        op.add_column(
            "ingestion_contracts",
            sa.Column("late_arrival_policy", sa.String(30), nullable=False, server_default="strict"),
        )
    if "idempotency_strategy" not in columns:
        op.add_column(
            "ingestion_contracts",
            sa.Column(
                "idempotency_strategy", sa.String(50), nullable=False,
                server_default="primary_key_upsert",
            ),
        )


def downgrade() -> None:
    op.drop_column("ingestion_contracts", "idempotency_strategy")
    op.drop_column("ingestion_contracts", "late_arrival_policy")
    op.drop_column("ingestion_contracts", "initial_watermark")
