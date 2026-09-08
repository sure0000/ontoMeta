"""superset assets: Superset 资产登记簿

Revision ID: superset_assets_20260908
Revises: 61746a99b7fa
Create Date: 2026-09-08
"""

from alembic import op
import sqlalchemy as sa

revision = "superset_assets_20260908"
down_revision = "61746a99b7fa"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "superset_assets",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("asset_type", sa.String(length=16), nullable=False),
        sa.Column("superset_id", sa.Integer(), nullable=False),
        sa.Column("embedded_uuid", sa.String(length=64), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("url_path", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("viz_type", sa.String(length=64), nullable=True),
        sa.Column("dataset_ref", sa.String(length=128), nullable=True),
        sa.Column("superset_dataset_id", sa.Integer(), nullable=True),
        sa.Column("domain_id", sa.String(length=36), nullable=True),
        sa.Column("ontology_id", sa.String(length=36), nullable=True),
        sa.Column("created_by", sa.String(length=255), nullable=True),
        sa.Column(
            "created_via", sa.String(length=16), nullable=False, server_default="mcp"
        ),
        sa.Column(
            "state", sa.String(length=16), nullable=False, server_default="unknown"
        ),
        sa.Column("last_seen_at", sa.DateTime(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("extra_json", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["domain_id"], ["domain_contexts.id"]),
        sa.UniqueConstraint(
            "asset_type", "superset_id", name="uq_superset_assets_type_id"
        ),
    )
    op.create_index(
        "ix_superset_assets_asset_type", "superset_assets", ["asset_type"]
    )
    op.create_index(
        "ix_superset_assets_superset_id", "superset_assets", ["superset_id"]
    )
    op.create_index(
        "ix_superset_assets_dataset_ref", "superset_assets", ["dataset_ref"]
    )
    op.create_index(
        "ix_superset_assets_superset_dataset_id",
        "superset_assets",
        ["superset_dataset_id"],
    )
    op.create_index("ix_superset_assets_domain_id", "superset_assets", ["domain_id"])
    op.create_index(
        "ix_superset_assets_ontology_id", "superset_assets", ["ontology_id"]
    )


def downgrade() -> None:
    for name in (
        "ix_superset_assets_ontology_id",
        "ix_superset_assets_domain_id",
        "ix_superset_assets_superset_dataset_id",
        "ix_superset_assets_dataset_ref",
        "ix_superset_assets_superset_id",
        "ix_superset_assets_asset_type",
    ):
        op.drop_index(name, table_name="superset_assets")
    op.drop_table("superset_assets")
