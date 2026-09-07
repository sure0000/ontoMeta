"""智能关系补充：异步推断任务表

Revision ID: d7b5276879d6
Revises: 8d4d847200c9
Create Date: 2026-09-07

LLM 判定实测 ~430s，画布按钮不能同步等——推断落成任务 + 轮询进度。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "d7b5276879d6"
down_revision = "8d4d847200c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "relation_inference_tasks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "domain_context_id",
            sa.String(36),
            sa.ForeignKey("domain_contexts.id"),
            nullable=False,
        ),
        sa.Column("run_id", sa.String(36), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
        sa.Column("progress", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("summary_json", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "ix_relation_inference_tasks_domain_context_id",
        "relation_inference_tasks",
        ["domain_context_id"],
    )
    op.create_index(
        "ix_relation_inference_tasks_run_id", "relation_inference_tasks", ["run_id"]
    )
    op.create_index(
        "ix_relation_inference_tasks_status", "relation_inference_tasks", ["status"]
    )


def downgrade() -> None:
    op.drop_index("ix_relation_inference_tasks_status", "relation_inference_tasks")
    op.drop_index("ix_relation_inference_tasks_run_id", "relation_inference_tasks")
    op.drop_index(
        "ix_relation_inference_tasks_domain_context_id", "relation_inference_tasks"
    )
    op.drop_table("relation_inference_tasks")
