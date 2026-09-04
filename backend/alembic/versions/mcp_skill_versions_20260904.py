"""mcp: append-only Skill version snapshots

Revision ID: mcp_skill_versions_20260904
Revises: mcp_skills_20260904
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "mcp_skill_versions_20260904"
down_revision: Union[str, Sequence[str], None] = "mcp_skills_20260904"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if "mcp_skill_versions" in sa.inspect(bind).get_table_names():
        return
    op.create_table(
        "mcp_skill_versions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("skill_name", sa.String(120), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("body_md", sa.Text(), nullable=False),
        sa.Column("action", sa.String(20), nullable=False),
        sa.Column("created_by", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("builtin_digest", sa.String(64), nullable=True),
    )
    op.create_index("ix_mcp_skill_versions_skill_name", "mcp_skill_versions", ["skill_name"])
    op.create_index("ix_mcp_skill_versions_version", "mcp_skill_versions", ["version"])
    with op.batch_alter_table("mcp_skill_versions", schema=None) as batch_op:
        batch_op.create_unique_constraint(
            "uq_mcp_skill_versions_skill_version", ["skill_name", "version"]
        )


def downgrade() -> None:
    bind = op.get_bind()
    if "mcp_skill_versions" not in sa.inspect(bind).get_table_names():
        return
    with op.batch_alter_table("mcp_skill_versions", schema=None) as batch_op:
        batch_op.drop_constraint("uq_mcp_skill_versions_skill_version", type_="unique")
    op.drop_index("ix_mcp_skill_versions_version", table_name="mcp_skill_versions")
    op.drop_index("ix_mcp_skill_versions_skill_name", table_name="mcp_skill_versions")
    op.drop_table("mcp_skill_versions")
