"""align timestamp nullability with ORM metadata

Revision ID: 8d31c6f0a2b4
Revises: 2f7c9a1d4e6b
Create Date: 2026-08-22

Several historical create-table revisions supplied a server default for timestamp
columns but omitted ``nullable=False``.  SQLAlchemy's typed ORM declarations infer
those columns as non-nullable, so every clean Alembic database retained schema drift.
Backfill the unlikely legacy NULL values before enforcing the model invariant.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "8d31c6f0a2b4"
down_revision: Union[str, Sequence[str], None] = "2f7c9a1d4e6b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Group columns by table so SQLite only rebuilds each table once.
_TIMESTAMP_COLUMNS: dict[str, tuple[str, ...]] = {
    "chat_bi_conversation_tasks": ("created_at",),
    "chat_bi_decision_records": ("created_at",),
    "chat_bi_domain_memory": ("last_used_at", "created_at"),
    "cube_settings": ("updated_at",),
    "data_app_datasets": ("created_at", "updated_at"),
    "data_app_versions": ("created_at",),
    "data_app_widgets": ("created_at", "updated_at"),
    "data_apps": ("created_at", "updated_at"),
    "data_sources": ("created_at", "updated_at"),
    "governance_standard_records": ("created_at",),
    "governance_task_pipeline_steps": ("created_at", "updated_at"),
    "governance_task_pipelines": ("created_at", "updated_at"),
    "semantic_index_entries": ("created_at",),
}


def upgrade() -> None:
    """Backfill legacy NULL timestamps and enforce NOT NULL."""
    bind = op.get_bind()
    for table, columns in _TIMESTAMP_COLUMNS.items():
        assignments = ", ".join(
            f"{column} = COALESCE({column}, CURRENT_TIMESTAMP)"
            for column in columns
        )
        predicate = " OR ".join(f"{column} IS NULL" for column in columns)
        bind.execute(sa.text(f"UPDATE {table} SET {assignments} WHERE {predicate}"))

        with op.batch_alter_table(table) as batch_op:
            for column in columns:
                batch_op.alter_column(
                    column,
                    existing_type=sa.DateTime(),
                    nullable=False,
                )


def downgrade() -> None:
    """Restore the historical nullable timestamp declarations."""
    for table, columns in reversed(tuple(_TIMESTAMP_COLUMNS.items())):
        with op.batch_alter_table(table) as batch_op:
            for column in columns:
                batch_op.alter_column(
                    column,
                    existing_type=sa.DateTime(),
                    nullable=True,
                )
