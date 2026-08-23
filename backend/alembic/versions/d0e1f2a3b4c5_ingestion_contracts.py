"""Flink ingestion contracts for source-to-Doris ODS.

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d0e1f2a3b4c5"
down_revision: Union[str, Sequence[str], None] = "c9d0e1f2a3b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if "ingestion_contracts" in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    op.create_table(
        "ingestion_contracts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("ontology_id", sa.String(36), nullable=False),
        sa.Column("ontology_version", sa.Integer(), nullable=False),
        sa.Column("object_type_id", sa.String(36), nullable=False),
        sa.Column("source_datasource_id", sa.String(36), nullable=False),
        sa.Column("source_physical_table", sa.String(512), nullable=False),
        sa.Column("source_mapping_json", sa.Text(), nullable=True),
        sa.Column("doris_datasource_id", sa.String(36), nullable=False),
        sa.Column("target_ods_database", sa.String(255), nullable=False),
        sa.Column("target_ods_table", sa.String(255), nullable=False),
        sa.Column("mode", sa.String(20), nullable=False, server_default="full"),
        sa.Column("primary_keys_json", sa.Text(), nullable=True),
        sa.Column("sequence_column", sa.String(255), nullable=True),
        sa.Column("incremental_column", sa.String(255), nullable=True),
        sa.Column("initial_watermark", sa.String(255), nullable=True),
        sa.Column("late_arrival_policy", sa.String(30), nullable=False, server_default="strict"),
        sa.Column("idempotency_strategy", sa.String(50), nullable=False, server_default="primary_key_upsert"),
        sa.Column("delete_policy", sa.String(30), nullable=False, server_default="ignore"),
        sa.Column("refresh_cron", sa.String(100), nullable=True),
        sa.Column("flink_params_json", sa.Text(), nullable=True),
        sa.Column("status", sa.String(30), nullable=False, server_default="draft"),
        sa.Column("last_success_at", sa.DateTime(), nullable=True),
        sa.Column("sync_watermark", sa.String(255), nullable=True),
        sa.Column("flink_job_id", sa.String(255), nullable=True),
        sa.Column("checkpoint_path", sa.Text(), nullable=True),
        sa.Column("savepoint_path", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=True, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["ontology_id"], ["ontologies.id"]),
        sa.ForeignKeyConstraint(["object_type_id"], ["object_types.id"]),
        sa.ForeignKeyConstraint(["source_datasource_id"], ["data_sources.id"]),
        sa.ForeignKeyConstraint(["doris_datasource_id"], ["data_sources.id"]),
        sa.UniqueConstraint(
            "ontology_id", "ontology_version", "object_type_id",
            name="uq_ingestion_contract_object_version",
        ),
    )
    op.create_index("ix_ingestion_contracts_ontology_id", "ingestion_contracts", ["ontology_id"])
    op.create_index("ix_ingestion_contracts_object_type_id", "ingestion_contracts", ["object_type_id"])
    op.create_index("ix_ingestion_contracts_source_datasource_id", "ingestion_contracts", ["source_datasource_id"])
    op.create_index("ix_ingestion_contracts_doris_datasource_id", "ingestion_contracts", ["doris_datasource_id"])


def downgrade() -> None:
    op.drop_table("ingestion_contracts")
