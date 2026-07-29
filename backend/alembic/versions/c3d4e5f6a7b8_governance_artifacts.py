"""agent pipeline: governance_artifacts 表

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-07-29

M5：治理制品。把「LLM 草稿 → 校验 → 人工确认 → 执行 → 溯源」从本体一种制品
泛化到集群拓扑/同步作业/ETL/指标四种，共用同一条流水线。

ontology_id 可空且不设外键：cluster 制品没有本体归属。
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, Sequence[str], None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "governance_artifacts"


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _TABLE in insp.get_table_names():
        return

    op.create_table(
        _TABLE,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("kind", sa.String(30), nullable=False, index=True),
        sa.Column("name", sa.String(255), nullable=False, server_default=""),
        sa.Column("ontology_id", sa.String(36), nullable=True, index=True),
        sa.Column("intent", sa.Text(), nullable=True),
        sa.Column("spec_json", sa.Text(), nullable=True),
        sa.Column("status", sa.String(30), nullable=False, server_default="drafted"),
        sa.Column("validation_report_json", sa.Text(), nullable=True),
        sa.Column("execution_receipt_json", sa.Text(), nullable=True),
        sa.Column("confirmed_by", sa.String(255), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(), nullable=True),
        sa.Column("executed_at", sa.DateTime(), nullable=True),
        # 溯源
        sa.Column("origin", sa.String(30), nullable=False, server_default="machine"),
        sa.Column("overridden_fields", sa.Text(), nullable=True),
        sa.Column("machine_baseline", sa.Text(), nullable=True),
        sa.Column("user_created", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("deleted_by_user", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("upstream_removed", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("last_generation_id", sa.String(36), nullable=True),
        sa.Column("conflict_json", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_governance_artifacts_status", _TABLE, ["status"])


def downgrade() -> None:
    op.drop_index("ix_governance_artifacts_status", table_name=_TABLE)
    op.drop_table(_TABLE)
