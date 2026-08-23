"""Doris ADS projections for metrics/tags/rules.

Revision ID: d2f3a4b5c6d7
Revises: d1f2a3b4c5d6
"""
from __future__ import annotations
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op

revision: str = "d2f3a4b5c6d7"
down_revision: Union[str, Sequence[str], None] = "d1f2a3b4c5d6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "warehouse_logic_projections" in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    op.create_table(
        "warehouse_logic_projections",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("deployment_id", sa.String(36), nullable=False),
        sa.Column("business_logic_id", sa.String(36), nullable=False),
        sa.Column("serving_database", sa.String(255), nullable=False),
        sa.Column("serving_table", sa.String(255), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("queryable", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_success_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=True, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["deployment_id"], ["ontology_warehouse_deployments.id"]),
        sa.ForeignKeyConstraint(["business_logic_id"], ["business_logics.id"]),
        sa.UniqueConstraint("deployment_id", "business_logic_id", name="uq_warehouse_logic_projection"),
    )
    op.create_index("ix_warehouse_logic_projections_deployment_id", "warehouse_logic_projections", ["deployment_id"])
    op.create_index("ix_warehouse_logic_projections_business_logic_id", "warehouse_logic_projections", ["business_logic_id"])
    op.create_index("ix_warehouse_logic_projections_queryable", "warehouse_logic_projections", ["queryable"])


def downgrade() -> None:
    op.drop_table("warehouse_logic_projections")
