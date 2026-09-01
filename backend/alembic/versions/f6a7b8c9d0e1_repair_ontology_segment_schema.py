"""Repair partial ontology segment migration on existing databases.

Some installations recorded the segment migration as applied after running a
partial SQLite schema operation.  Keep this migration idempotent so clean
databases are unchanged while existing databases receive the missing relation
review column and index.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "f6a7b8c9d0e1"
down_revision: Union[str, Sequence[str], None] = "f5a6b7c8d9e0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("relation_types")}
    if "needs_review" not in columns:
        with op.batch_alter_table("relation_types") as batch_op:
            batch_op.add_column(
                sa.Column("needs_review", sa.Boolean(), nullable=False, server_default="0")
            )

    inspector = sa.inspect(bind)
    indexes = {index["name"] for index in inspector.get_indexes("relation_types")}
    if "ix_relation_types_needs_review" not in indexes:
        op.create_index(
            "ix_relation_types_needs_review",
            "relation_types",
            ["needs_review"],
            unique=False,
        )

    # member_count is derived data; repair stale counts without inventing
    # assignments for objects that have not been clustered yet.
    bind.execute(
        sa.text(
            "UPDATE ontology_segments SET member_count = "
            "(SELECT COUNT(*) FROM object_types "
            "WHERE object_types.segment_id = ontology_segments.id)"
        )
    )


def downgrade() -> None:
    # The parent migration owns the column.  This repair is intentionally a
    # no-op on downgrade to avoid deleting data from a valid parent schema.
    pass
