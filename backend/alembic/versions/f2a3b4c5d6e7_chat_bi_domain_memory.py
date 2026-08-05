"""chat_bi_domain_memory: 按域沉淀的高频使用记忆（P3 跨会话记忆）

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-08-05

每次 Data Agent 给出已接地回答后，把命中的对象/口径按 (域, 实体) 累加计数，形成「本域实际
常被问什么」的动态画像；召回时取 top-N 作软提示注入系统提示，让复现问题少绕检索、少重复澄清。

作用域=数据域（本系统按角色鉴权、无逐用户身份）。ref_id 软引用（不设 FK）：实体权威在本体侧。
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f2a3b4c5d6e7"
down_revision: Union[str, Sequence[str], None] = "e1f2a3b4c5d6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "chat_bi_domain_memory" in set(insp.get_table_names()):
        return
    op.create_table(
        "chat_bi_domain_memory",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("domain_id", sa.String(length=36), nullable=False),
        sa.Column("ref_kind", sa.String(length=30), nullable=False),
        sa.Column("ref_id", sa.String(length=36), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=True),
        sa.Column("hit_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_used_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["domain_id"], ["domain_contexts.id"]),
        sa.UniqueConstraint(
            "domain_id", "ref_kind", "ref_id", name="uq_chat_bi_domain_memory_ref"
        ),
    )
    op.create_index(
        "ix_chat_bi_domain_memory_domain_id",
        "chat_bi_domain_memory",
        ["domain_id"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "chat_bi_domain_memory" not in set(insp.get_table_names()):
        return
    op.drop_table("chat_bi_domain_memory")
