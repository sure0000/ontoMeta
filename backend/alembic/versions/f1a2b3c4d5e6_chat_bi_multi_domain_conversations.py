"""chat-bi multi-domain conversations: add domain_ids_json, relax domain_id

Revision ID: f1a2b3c4d5e6
Revises: d4e5f6a7b8ca
Create Date: 2026-08-11

Data Agent 支持多数据域 / 不选域（全域通盘）：
- 新增 chat_bi_conversations.domain_ids_json（JSON 数组字符串，可空）。
  非空 = 跨域会话；空/NULL = 不选域（全域通盘）。
- domain_id 放宽为可空（保留作锚点/兼容；旧数据迁移为 domain_ids_json=[domain_id]）。
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f1a2b3c4d5e6"
down_revision: Union[str, Sequence[str], None] = "d4e5f6a7b8ca"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("chat_bi_conversations")}

    if "domain_ids_json" not in cols:
        op.add_column(
            "chat_bi_conversations",
            sa.Column("domain_ids_json", sa.Text(), nullable=True),
        )

    # 旧会话迁移：domain_ids_json 为空且 domain_id 非空 → domain_ids_json = ["<domain_id>"]
    bind.execute(
        sa.text(
            "UPDATE chat_bi_conversations "
            "SET domain_ids_json = '[' || '\"' || domain_id || '\"' || ']' "
            "WHERE (domain_ids_json IS NULL OR domain_ids_json = '') "
            "AND domain_id IS NOT NULL"
        )
    )

    # 放宽 domain_id 为可空。SQLite 不支持直接 ALTER COLUMN，用 batch_alter_table 重建。
    with op.batch_alter_table("chat_bi_conversations") as batch_op:
        batch_op.alter_column(
            "domain_id",
            existing_type=sa.String(length=36),
            nullable=True,
        )


def downgrade() -> None:
    with op.batch_alter_table("chat_bi_conversations") as batch_op:
        batch_op.alter_column(
            "domain_id",
            existing_type=sa.String(length=36),
            nullable=False,
        )
    op.drop_column("chat_bi_conversations", "domain_ids_json")
