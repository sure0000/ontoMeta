"""Ensure Doris ontology deployment/projection tables exist on databases upgraded to c5.

Revision ID: c6d7e8f9a0b1
Revises: c5d6e7f8a9b0
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c9d0e1f2a3b4"
down_revision: Union[str, Sequence[str], None] = "c5d6e7f8a9b0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    # c5 added these columns nullable on some already-upgraded SQLite files;
    # normalize them to the ORM contract before creating projection tables.
    cols = {c["name"]: c for c in sa.inspect(bind).get_columns("data_sources")}
    for name, default in (("purpose", "business_source"), ("is_default_warehouse", "0"), ("enabled", "1")):
        if name in cols and cols[name]["nullable"]:
            with op.batch_alter_table("data_sources") as batch:
                batch.alter_column(name, nullable=False, existing_type=cols[name]["type"], existing_server_default=default)
    tables = set(sa.inspect(bind).get_table_names())
    if "ontology_warehouse_deployments" not in tables:
        op.create_table(
            "ontology_warehouse_deployments",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("ontology_id", sa.String(36), nullable=False),
            sa.Column("ontology_version", sa.Integer(), nullable=False),
            sa.Column("doris_datasource_id", sa.String(36), nullable=False),
            sa.Column("status", sa.String(30), nullable=False, server_default="pending"),
            sa.Column("materialization_artifact_id", sa.String(36), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=True, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["ontology_id"], ["ontologies.id"]),
            sa.ForeignKeyConstraint(["doris_datasource_id"], ["data_sources.id"]),
            sa.UniqueConstraint("ontology_id", "ontology_version", name="uq_ontology_warehouse_version"),
        )
    if "warehouse_object_projections" not in tables:
        op.create_table(
            "warehouse_object_projections",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("deployment_id", sa.String(36), nullable=False),
            sa.Column("object_type_id", sa.String(36), nullable=False),
            sa.Column("ods_database", sa.String(255), nullable=True),
            sa.Column("ods_table", sa.String(255), nullable=True),
            sa.Column("serving_layer", sa.String(20), nullable=True),
            sa.Column("serving_database", sa.String(255), nullable=True),
            sa.Column("serving_table", sa.String(255), nullable=True),
            sa.Column("column_mapping_json", sa.Text(), nullable=True),
            sa.Column("schema_status", sa.String(20), nullable=False, server_default="pending"),
            sa.Column("sync_status", sa.String(20), nullable=False, server_default="empty"),
            sa.Column("transform_status", sa.String(20), nullable=False, server_default="not_required"),
            sa.Column("last_sync_at", sa.DateTime(), nullable=True),
            sa.Column("sync_watermark", sa.String(255), nullable=True),
            sa.Column("queryable", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(), nullable=True, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=True, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["deployment_id"], ["ontology_warehouse_deployments.id"]),
            sa.ForeignKeyConstraint(["object_type_id"], ["object_types.id"]),
            sa.UniqueConstraint("deployment_id", "object_type_id", name="uq_warehouse_projection_object"),
        )


def downgrade() -> None:
    op.drop_table("warehouse_object_projections")
    op.drop_table("ontology_warehouse_deployments")
