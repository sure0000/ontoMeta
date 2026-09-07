"""血缘补录：SQL 表名 → URN 的人工映射

Revision ID: db3055d82d31
Revises: c086109d5cb1
Create Date: 2026-09-07

blocked 边此前只能重扫，而重扫用同一套 resolve、结果一样。给人一个说「这张表其实是那张」
的地方，域级复用，重扫时自动套用。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "db3055d82d31"
down_revision = "c086109d5cb1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "lineage_table_mappings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "domain_context_id",
            sa.String(36),
            sa.ForeignKey("domain_contexts.id"),
            nullable=False,
        ),
        sa.Column("sql_table", sa.String(512), nullable=False),
        sa.Column("target_urn", sa.String(1024), nullable=False),
        sa.Column("target_table", sa.String(512), nullable=False),
        sa.Column("created_by", sa.String(255), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "domain_context_id", "sql_table", name="uq_lineage_mapping_domain_table"
        ),
    )
    op.create_index(
        "ix_lineage_table_mappings_domain_context_id",
        "lineage_table_mappings",
        ["domain_context_id"],
    )
    op.create_index(
        "ix_lineage_table_mappings_sql_table", "lineage_table_mappings", ["sql_table"]
    )


def downgrade() -> None:
    op.drop_index("ix_lineage_table_mappings_sql_table", "lineage_table_mappings")
    op.drop_index(
        "ix_lineage_table_mappings_domain_context_id", "lineage_table_mappings"
    )
    op.drop_table("lineage_table_mappings")
