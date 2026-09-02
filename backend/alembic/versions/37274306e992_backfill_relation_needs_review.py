"""Backfill relation review debt so the relation queue reflects reality.

``relation_types.needs_review`` was added with server_default 0 and nothing ever
wrote it, so the column read as "everything reviewed" while thousands of
machine-suggested relations had never been looked at by a human.  The review
queue reads that column, so without this backfill the relation lane is
permanently empty on existing databases.

Scope is deliberately narrow: machine-generated relations still sitting in
``suggested`` — never edited, never published, i.e. nobody ever confirmed them.
Published or human-edited relations are left alone.

Note this does **not** affect publishing: relation publication is gated on both
endpoints being confirmed business objects, never on the relation's own review
flag (see services/publish.select_publishable).
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "37274306e992"
down_revision: Union[str, Sequence[str], None] = "f6a7b8c9d0e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_relation_review_column(bind) -> bool:
    inspector = sa.inspect(bind)
    if "relation_types" not in inspector.get_table_names():
        return False
    return "needs_review" in {col["name"] for col in inspector.get_columns("relation_types")}


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_relation_review_column(bind):
        return
    bind.execute(
        sa.text(
            """
            UPDATE relation_types
               SET needs_review = true
             WHERE needs_review = false
               AND origin = 'machine'
               AND status = 'suggested'
               AND deleted_by_user = false
            """
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    if not _has_relation_review_column(bind):
        return
    # 只回退本次回填的那一批（同样的判据），不碰人工确认过的关系。
    bind.execute(
        sa.text(
            """
            UPDATE relation_types
               SET needs_review = false
             WHERE needs_review = true
               AND origin = 'machine'
               AND status = 'suggested'
               AND deleted_by_user = false
            """
        )
    )
