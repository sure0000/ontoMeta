"""mcp flow forms: 交互式建数流程的一次性网页表单

Revision ID: mcp_flow_forms_20260905
Revises: mcp_agent_exec_approval_20260904
Create Date: 2026-09-05
"""

from alembic import op
import sqlalchemy as sa

revision = "mcp_flow_forms_20260905"
down_revision = "mcp_agent_exec_approval_20260904"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mcp_flow_forms",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("ontology_id", sa.String(length=36), nullable=True),
        sa.Column("stage", sa.String(length=32), nullable=False),
        sa.Column("goal", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "answers_json", sa.Text(), nullable=False, server_default="{}"
        ),
        sa.Column("submitted_json", sa.Text(), nullable=True),
        sa.Column(
            "status", sa.String(length=20), nullable=False, server_default="pending"
        ),
        sa.Column("created_by", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("submitted_at", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_mcp_flow_forms_status", "mcp_flow_forms", ["status", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_mcp_flow_forms_status", table_name="mcp_flow_forms")
    op.drop_table("mcp_flow_forms")
