"""chat_bi_decision_records: Data Agent 人工决策留痕（六环闭环）

Revision ID: 7ff98a08a656
Revises: 94ecd59449c8
Create Date: 2026-08-18

一次对话里人在关键节点拍的板：需求/本体/数据/执行方案/执行任务/结果确认。
追加式账本，记录不改写。

**为什么要这张表**：``GovernanceArtifact.confirmed_by`` 会被 ``agent_pipeline.edit()``
置空（改了 spec 旧确认即失效），``ChangeConfirmation`` 硬绑 ontology_id，
``agent_trace`` 是默认关闭的 JSONL。三者都答不了"这次对话里人到底点过什么"。

修订号用**随机 hex** 而非本仓惯用的顺序 hex（a1b2c3…/b2c3d4…）：那种走位极易撞号，
``d4e5f6a7b8c9`` 与 ``d4e5f6a7b8ca`` 就差一个字符地撞在一起过，症状是整套 pytest
炸成几百个看不出根因的 error。

只对 conversation_id 设 FK；ref_id/message_id 是软引用——产物的权威在各自模块。
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "7ff98a08a656"
down_revision: Union[str, Sequence[str], None] = "94ecd59449c8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "chat_bi_decision_records" in set(insp.get_table_names()):
        return
    op.create_table(
        "chat_bi_decision_records",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("conversation_id", sa.String(length=36), nullable=False),
        sa.Column("message_id", sa.String(length=36), nullable=True),
        sa.Column("block_id", sa.String(length=40), nullable=True),
        sa.Column("seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("node", sa.String(length=30), nullable=False),
        sa.Column("stage", sa.String(length=40), nullable=True),
        sa.Column("trigger", sa.String(length=40), nullable=True),
        sa.Column("outcome", sa.String(length=20), nullable=False),
        sa.Column("subject_id", sa.String(length=64), nullable=True),
        sa.Column("subject_role", sa.String(length=30), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("proposed_json", sa.Text(), nullable=True),
        sa.Column("chosen_json", sa.Text(), nullable=True),
        sa.Column("overridden_fields", sa.Text(), nullable=True),
        sa.Column("ref_kind", sa.String(length=40), nullable=True),
        sa.Column("ref_id", sa.String(length=36), nullable=True),
        sa.Column("dedup_key", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["conversation_id"], ["chat_bi_conversations.id"]),
        sa.UniqueConstraint("dedup_key", name="uq_chat_bi_decision_dedup"),
    )
    for col in ("conversation_id", "message_id", "node", "outcome",
                "subject_id", "ref_kind", "ref_id", "created_at"):
        op.create_index(
            f"ix_chat_bi_decision_records_{col}", "chat_bi_decision_records", [col]
        )
    op.create_index(
        "ix_chat_bi_decision_conv_seq",
        "chat_bi_decision_records",
        ["conversation_id", "seq"],
    )
    op.create_index(
        "ix_chat_bi_decision_node_created",
        "chat_bi_decision_records",
        ["node", "created_at"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "chat_bi_decision_records" not in set(insp.get_table_names()):
        return
    op.drop_table("chat_bi_decision_records")
