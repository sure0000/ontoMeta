"""B8: external app scopes, rate_limit, call logs

Revision ID: b8e0a1c2d3f4
Revises: 5a881e5c0024
Create Date: 2026-07-17
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b8e0a1c2d3f4"
down_revision: Union[str, Sequence[str], None] = "5a881e5c0024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("external_apps", schema=None) as batch_op:
        batch_op.add_column(sa.Column("scopes", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("rate_limit_per_minute", sa.Integer(), nullable=True))

    op.create_table(
        "external_api_call_logs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("app_id", sa.String(length=36), nullable=False),
        sa.Column("tool_name", sa.String(length=128), nullable=True),
        sa.Column("path", sa.String(length=255), nullable=True),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.ForeignKeyConstraint(["app_id"], ["external_apps.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("external_api_call_logs", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_external_api_call_logs_app_id"), ["app_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_external_api_call_logs_tool_name"), ["tool_name"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_external_api_call_logs_created_at"), ["created_at"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("external_api_call_logs", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_external_api_call_logs_created_at"))
        batch_op.drop_index(batch_op.f("ix_external_api_call_logs_tool_name"))
        batch_op.drop_index(batch_op.f("ix_external_api_call_logs_app_id"))
    op.drop_table("external_api_call_logs")

    with op.batch_alter_table("external_apps", schema=None) as batch_op:
        batch_op.drop_column("rate_limit_per_minute")
        batch_op.drop_column("scopes")
