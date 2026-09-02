"""Doris warehouse foundation: explicit datasource roles and connection contract.

Revision ID: c5d6e7f8a9b0
Revises: c4e19b7a5d02
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c5d6e7f8a9b0"
down_revision: Union[str, Sequence[str], None] = "c4e19b7a5d02"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {c["name"] for c in inspector.get_columns("data_sources")}
    if "purpose" not in columns:
        op.add_column(
            "data_sources",
            sa.Column("purpose", sa.String(30), nullable=False, server_default="business_source"),
        )
    if "is_default_warehouse" not in columns:
        op.add_column(
            "data_sources",
            sa.Column("is_default_warehouse", sa.Boolean(), nullable=False, server_default=sa.false()),
        )
    if "enabled" not in columns:
        op.add_column(
            "data_sources",
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        )

    bind.execute(
        sa.text(
            "UPDATE data_sources SET purpose = CASE "
            "WHEN lower(kind) = 'doris' AND (catalog_name IS NULL OR trim(catalog_name) IN ('', 'internal')) "
            "THEN 'warehouse' ELSE 'business_source' END"
        )
    )
    bind.execute(
        sa.text("UPDATE data_sources SET is_default_warehouse = false WHERE is_default_warehouse IS NULL")
    )
    bind.execute(sa.text("UPDATE data_sources SET enabled = true WHERE enabled IS NULL"))

    # Migration-only promotion. Runtime routing never chooses by recency.
    candidate = bind.execute(
        sa.text(
            "SELECT id FROM data_sources "
            "WHERE purpose='warehouse' AND lower(kind)='doris' "
            "AND dsn_secret_ref IS NOT NULL AND trim(dsn_secret_ref) <> '' "
            "ORDER BY updated_at DESC, created_at DESC, id DESC LIMIT 1"
        )
    ).scalar()
    if candidate:
        bind.execute(
            sa.text("UPDATE data_sources SET is_default_warehouse = 1 WHERE id = :id"),
            {"id": candidate},
        )

    indexes = {i["name"] for i in sa.inspect(bind).get_indexes("data_sources")}
    if "uq_data_sources_default_warehouse" not in indexes and bind.dialect.name in {
        "sqlite",
        "postgresql",
    }:
        predicate = sa.text(
            "is_default_warehouse = 1"
            if bind.dialect.name == "sqlite"
            else "is_default_warehouse = true"
        )
        kwargs = {"sqlite_where": predicate} if bind.dialect.name == "sqlite" else {
            "postgresql_where": predicate
        }
        op.create_index(
            "uq_data_sources_default_warehouse",
            "data_sources",
            ["is_default_warehouse"],
            unique=True,
            **kwargs,
        )

    if "doris_warehouse_configs" not in set(sa.inspect(bind).get_table_names()):
        op.create_table(
            "doris_warehouse_configs",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("warehouse_datasource_id", sa.String(36), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("query_host", sa.String(255), nullable=True),
            sa.Column("query_port", sa.Integer(), nullable=False, server_default="9030"),
            sa.Column("default_catalog", sa.String(100), nullable=False, server_default="internal"),
            sa.Column("default_database", sa.String(255), nullable=True),
            sa.Column("connect_timeout_seconds", sa.Integer(), nullable=False, server_default="10"),
            sa.Column("query_timeout_seconds", sa.Integer(), nullable=False, server_default="15"),
            sa.Column("ssl_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("fenodes_json", sa.Text(), nullable=True),
            sa.Column("airflow_ddl_conn_id", sa.String(255), nullable=True),
            sa.Column("airflow_etl_conn_id", sa.String(255), nullable=True),
            sa.Column("airflow_flink_conn_id", sa.String(255), nullable=True),
            sa.Column("reader_dsn_secret_ref", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=True, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["warehouse_datasource_id"], ["data_sources.id"]),
            sa.UniqueConstraint("warehouse_datasource_id", name="uq_doris_config_datasource"),
        )


def downgrade() -> None:
    op.drop_table("doris_warehouse_configs")
    if op.get_bind().dialect.name in {"sqlite", "postgresql"}:
        op.drop_index("uq_data_sources_default_warehouse", table_name="data_sources")
    op.drop_column("data_sources", "enabled")
    op.drop_column("data_sources", "is_default_warehouse")
    op.drop_column("data_sources", "purpose")
