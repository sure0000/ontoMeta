"""chat_bi_conversation_tasks: 会话 ↔ 数据任务关联（P1 跨轮任务记忆）

Revision ID: e1f2a3b4c5d6
Revises: a7b8c9d0e1f2
Create Date: 2026-08-05

用户在某会话里对 Data Agent 的任务提案点「去校验并执行」建出治理制品后，前端把
(会话, 制品) 关联落这张表；后续该会话问「那个任务好了吗」时，get_task_status 无需用户
重报 id 即可解析出本会话产出的任务并回读实时状态。

artifact_id 用软引用（不设 FK）：治理制品与 chat 解耦，制品权威在 agent 侧，这里只记
「哪个会话催生了哪个任务」。
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e1f2a3b4c5d6"
down_revision: Union[str, Sequence[str], None] = "a7b8c9d0e1f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "chat_bi_conversation_tasks" in set(insp.get_table_names()):
        return
    op.create_table(
        "chat_bi_conversation_tasks",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("conversation_id", sa.String(length=36), nullable=False),
        sa.Column("artifact_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=True),
        sa.Column("intent", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["conversation_id"], ["chat_bi_conversations.id"]),
    )
    op.create_index(
        "ix_chat_bi_conversation_tasks_conversation_id",
        "chat_bi_conversation_tasks",
        ["conversation_id"],
    )
    op.create_index(
        "ix_chat_bi_conversation_tasks_artifact_id",
        "chat_bi_conversation_tasks",
        ["artifact_id"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "chat_bi_conversation_tasks" not in set(insp.get_table_names()):
        return
    op.drop_table("chat_bi_conversation_tasks")
