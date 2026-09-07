"""智能关系补充：键族候选表

Revision ID: 8d4d847200c9
Revises: artifact_result_verdict_20260905
Create Date: 2026-09-07

一个键族一行（不是一对关系一行）：两两展开是 O(n²)，jwsp 实测 12 个族展开是 15244 对，
那不是给人审的粒度。两两关系在 apply 时按需展开。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "8d4d847200c9"
down_revision = "artifact_result_verdict_20260905"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "relation_candidate_families",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "domain_context_id",
            sa.String(36),
            sa.ForeignKey("domain_contexts.id"),
            nullable=False,
        ),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("family_id", sa.String(64), nullable=False),
        sa.Column("value_shape", sa.String(64), nullable=False),
        # 下面几列在模型里是非可空（``Mapped[str]``/``Mapped[int]`` 不带 ``| None``），
        # 迁移必须对齐，否则 tests/test_migrations_cover_models.py 的 `alembic check` 会红。
        sa.Column("sample_values_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("members_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("table_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("column_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("verdict", sa.String(20), nullable=False),
        sa.Column("entity_name", sa.String(255), nullable=True),
        sa.Column("key_name", sa.String(255), nullable=True),
        sa.Column("predicate", sa.String(64), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0.5"),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("state", sa.String(20), nullable=False, server_default="proposed"),
        sa.Column("decided_by", sa.String(255), nullable=True),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "ix_relation_candidate_families_domain_context_id",
        "relation_candidate_families",
        ["domain_context_id"],
    )
    op.create_index(
        "ix_relation_candidate_families_run_id",
        "relation_candidate_families",
        ["run_id"],
    )
    op.create_index(
        "ix_relation_candidate_families_family_id",
        "relation_candidate_families",
        ["family_id"],
    )
    op.create_index(
        "ix_relation_candidate_families_verdict",
        "relation_candidate_families",
        ["verdict"],
    )
    op.create_index(
        "ix_relation_candidate_families_state",
        "relation_candidate_families",
        ["state"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_relation_candidate_families_state", "relation_candidate_families"
    )
    op.drop_index(
        "ix_relation_candidate_families_verdict", "relation_candidate_families"
    )
    op.drop_index(
        "ix_relation_candidate_families_family_id", "relation_candidate_families"
    )
    op.drop_index("ix_relation_candidate_families_run_id", "relation_candidate_families")
    op.drop_index(
        "ix_relation_candidate_families_domain_context_id",
        "relation_candidate_families",
    )
    op.drop_table("relation_candidate_families")
