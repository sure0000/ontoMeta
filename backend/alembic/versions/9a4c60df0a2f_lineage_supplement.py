"""血缘补录：代码包与它扫出来的边

补录是长期活，同一个域会陆续收到好几个 SQL 代码包，扫完不一定当场上报。包与边都
要留档：DataHub 那边只有边本身，没有「这条边是哪个包给的」。

Revision ID: 9a4c60df0a2f
Revises: 8c8a5b82add9
Create Date: 2026-09-03
"""

from alembic import op
import sqlalchemy as sa

revision = "9a4c60df0a2f"
down_revision = "8c8a5b82add9"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    bind = op.get_bind()
    return name in sa.inspect(bind).get_table_names()


def upgrade() -> None:
    if not _has_table("lineage_packages"):
        op.create_table(
            "lineage_packages",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "domain_context_id",
                sa.String(length=36),
                sa.ForeignKey("domain_contexts.id"),
                nullable=False,
            ),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("kind", sa.String(length=16), nullable=False, server_default="scan"),
            sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("dialect", sa.String(length=32), nullable=False, server_default="mysql"),
            sa.Column("archive_path", sa.String(length=1024), nullable=True),
            sa.Column("sql_files", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("directories", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("statements", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("parsed_files", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("failures_json", sa.Text(), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="scanned"),
            sa.Column("applied_edges", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("applied_resolved", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("applied_at", sa.DateTime(), nullable=True),
            sa.Column("uploaded_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("scanned_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        )
        op.create_index(
            "ix_lineage_packages_domain_context_id",
            "lineage_packages",
            ["domain_context_id"],
        )
        op.create_index("ix_lineage_packages_status", "lineage_packages", ["status"])
        op.create_index("ix_lineage_packages_kind", "lineage_packages", ["kind"])

    if not _has_table("lineage_package_edges"):
        op.create_table(
            "lineage_package_edges",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "package_id",
                sa.String(length=36),
                sa.ForeignKey("lineage_packages.id"),
                nullable=False,
            ),
            sa.Column("source_table", sa.String(length=512), nullable=False),
            sa.Column("target_table", sa.String(length=512), nullable=False),
            sa.Column("join_key", sa.String(length=1024), nullable=True),
            sa.Column("source_file", sa.String(length=1024), nullable=False),
            sa.Column("source_urn", sa.String(length=1024), nullable=True),
            sa.Column("target_urn", sa.String(length=1024), nullable=True),
            sa.Column("state", sa.String(length=20), nullable=False, server_default="ok"),
            sa.Column("reason", sa.String(length=255), nullable=True),
            sa.Column("applied_at", sa.DateTime(), nullable=True),
        )
        op.create_index(
            "ix_lineage_package_edges_package_id", "lineage_package_edges", ["package_id"]
        )
        op.create_index(
            "ix_lineage_package_edges_target_table",
            "lineage_package_edges",
            ["target_table"],
        )
        op.create_index(
            "ix_lineage_package_edges_state", "lineage_package_edges", ["state"]
        )


def downgrade() -> None:
    if _has_table("lineage_package_edges"):
        op.drop_table("lineage_package_edges")
    if _has_table("lineage_packages"):
        op.drop_table("lineage_packages")
