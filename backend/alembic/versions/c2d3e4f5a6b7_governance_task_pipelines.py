"""governance_task_pipelines: 多任务编排的任务链与步骤

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
Create Date: 2026-08-06

「物化 → 清洗 → 聚合」这种前后相继的任务此前只能一条条手建，链上的顺序与上下文全靠人记。
本迁移建两张表把链本身存下来：链（意图/归属本体）与步骤（序号/类型/意图/显式 context/
落成的制品 id）。

链的**状态不落库**，由各步制品的状态聚合推导——制品的权威在 governance_artifacts，
另存一份迟早分叉。故这里没有 status 列。
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c2d3e4f5a6b7"
down_revision: Union[str, Sequence[str], None] = "b1c2d3e4f5a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    if "governance_task_pipelines" not in existing:
        op.create_table(
            "governance_task_pipelines",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("name", sa.String(length=255), nullable=False, server_default=""),
            sa.Column("intent", sa.Text(), nullable=True),
            sa.Column("ontology_id", sa.String(length=36), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
        )
        op.create_index(
            "ix_governance_task_pipelines_ontology_id",
            "governance_task_pipelines",
            ["ontology_id"],
        )
    if "governance_task_pipeline_steps" not in existing:
        op.create_table(
            "governance_task_pipeline_steps",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "pipeline_id",
                sa.String(length=36),
                sa.ForeignKey("governance_task_pipelines.id"),
                nullable=False,
            ),
            sa.Column("step_index", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("kind", sa.String(length=30), nullable=False),
            sa.Column("intent", sa.Text(), nullable=False, server_default=""),
            sa.Column("context_json", sa.Text(), nullable=True),
            # 软引用：制品的权威在 governance_artifacts，这里只记「这一步落成了哪条」。
            sa.Column("artifact_id", sa.String(length=36), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
        )
        op.create_index(
            "ix_governance_task_pipeline_steps_pipeline_id",
            "governance_task_pipeline_steps",
            ["pipeline_id"],
        )
        op.create_index(
            "ix_governance_task_pipeline_steps_artifact_id",
            "governance_task_pipeline_steps",
            ["artifact_id"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    if "governance_task_pipeline_steps" in existing:
        op.drop_table("governance_task_pipeline_steps")
    if "governance_task_pipelines" in existing:
        op.drop_table("governance_task_pipelines")
