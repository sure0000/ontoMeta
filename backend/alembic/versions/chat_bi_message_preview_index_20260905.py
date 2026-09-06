"""add the composite index used by chat sidebar previews

Revision ID: cb_msg_preview_idx_20260905
Revises: mcp_flow_forms_20260905
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "cb_msg_preview_idx_20260905"
down_revision: Union[str, Sequence[str], None] = "mcp_flow_forms_20260905"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "chat_bi_messages"
_INDEX = "ix_chat_bi_messages_conversation_created"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return
    existing = {index["name"] for index in inspector.get_indexes(_TABLE)}
    if _INDEX not in existing:
        op.create_index(_INDEX, _TABLE, ["conversation_id", "created_at"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return
    existing = {index["name"] for index in inspector.get_indexes(_TABLE)}
    if _INDEX in existing:
        op.drop_index(_INDEX, table_name=_TABLE)
