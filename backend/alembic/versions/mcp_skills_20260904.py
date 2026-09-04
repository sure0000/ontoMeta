"""mcp: server-delivered skill overrides

Revision ID: mcp_skills_20260904
Revises: e2d48fc8520a
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "mcp_skills_20260904"
down_revision: Union[str, Sequence[str], None] = "e2d48fc8520a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if "mcp_skills" in sa.inspect(bind).get_table_names():
        return
    op.create_table(
        "mcp_skills",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("body_md", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("source", sa.String(20), nullable=False, server_default="builtin"),
        sa.Column("builtin_digest", sa.String(64), nullable=True),
        sa.Column("updated_by", sa.String(255), nullable=True),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_mcp_skills_name", "mcp_skills", ["name"], unique=True)


def downgrade() -> None:
    bind = op.get_bind()
    if "mcp_skills" not in sa.inspect(bind).get_table_names():
        return
    op.drop_index("ix_mcp_skills_name", table_name="mcp_skills")
    op.drop_table("mcp_skills")
