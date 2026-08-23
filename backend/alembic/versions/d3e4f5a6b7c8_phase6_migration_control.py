"""Phase 6 production migration control, evidence and approval records.

Revision ID: d3e4f5a6b7c8
Revises: d2f3a4b5c6d7
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d3e4f5a6b7c8"
down_revision: Union[str, Sequence[str], None] = "d2f3a4b5c6d7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "warehouse_migration_batches" not in tables:
        op.create_table(
            "warehouse_migration_batches",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("ontology_id", sa.String(36), nullable=False),
            sa.Column("ontology_version", sa.Integer(), nullable=False),
            sa.Column("doris_datasource_id", sa.String(36), nullable=True),
            sa.Column("status", sa.String(30), nullable=False, server_default="preparing"),
            sa.Column("current_step", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("approver", sa.String(255), nullable=False),
            sa.Column("rollback_owner", sa.String(255), nullable=False),
            sa.Column("observation_window_minutes", sa.Integer(), nullable=False),
            sa.Column("legacy_dag_ids_json", sa.Text(), nullable=True),
            sa.Column("new_dag_ids_json", sa.Text(), nullable=True),
            sa.Column("blocked_reason", sa.Text(), nullable=True),
            sa.Column("approved_by", sa.String(255), nullable=True),
            sa.Column("approval_note", sa.Text(), nullable=True),
            sa.Column("approved_at", sa.DateTime(), nullable=True),
            sa.Column("cutover_at", sa.DateTime(), nullable=True),
            sa.Column("observation_ends_at", sa.DateTime(), nullable=True),
            sa.Column("legacy_stopped_at", sa.DateTime(), nullable=True),
            sa.Column("completed_at", sa.DateTime(), nullable=True),
            sa.Column("rolled_back_at", sa.DateTime(), nullable=True),
            sa.Column("rollback_drill_json", sa.Text(), nullable=True),
            sa.Column("compatibility_items_json", sa.Text(), nullable=True),
            sa.Column("created_by", sa.String(255), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=True, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["ontology_id"], ["ontologies.id"]),
            sa.ForeignKeyConstraint(["doris_datasource_id"], ["data_sources.id"]),
        )
        op.create_index("ix_warehouse_migration_batches_ontology_id", "warehouse_migration_batches", ["ontology_id"])
        op.create_index("ix_warehouse_migration_batches_doris_datasource_id", "warehouse_migration_batches", ["doris_datasource_id"])
        op.create_index("ix_warehouse_migration_batches_status", "warehouse_migration_batches", ["status"])

    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "warehouse_migration_evidence" not in tables:
        op.create_table(
            "warehouse_migration_evidence",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("batch_id", sa.String(36), nullable=False),
            sa.Column("step", sa.Integer(), nullable=False),
            sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("status", sa.String(20), nullable=False),
            sa.Column("report_json", sa.Text(), nullable=False),
            sa.Column("artifact_ids_json", sa.Text(), nullable=True),
            sa.Column("checksum", sa.String(64), nullable=False),
            sa.Column("recorded_by", sa.String(255), nullable=False),
            sa.Column("recorded_at", sa.DateTime(), nullable=True, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["batch_id"], ["warehouse_migration_batches.id"]),
            sa.UniqueConstraint("batch_id", "step", "attempt", name="uq_warehouse_migration_step_attempt"),
        )
        op.create_index("ix_warehouse_migration_evidence_batch_id", "warehouse_migration_evidence", ["batch_id"])


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "warehouse_migration_evidence" in tables:
        op.drop_index("ix_warehouse_migration_evidence_batch_id", table_name="warehouse_migration_evidence")
        op.drop_table("warehouse_migration_evidence")
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "warehouse_migration_batches" in tables:
        op.drop_index("ix_warehouse_migration_batches_status", table_name="warehouse_migration_batches")
        op.drop_index("ix_warehouse_migration_batches_doris_datasource_id", table_name="warehouse_migration_batches")
        op.drop_index("ix_warehouse_migration_batches_ontology_id", table_name="warehouse_migration_batches")
        op.drop_table("warehouse_migration_batches")
