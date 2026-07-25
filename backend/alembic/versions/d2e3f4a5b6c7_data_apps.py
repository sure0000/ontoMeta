"""data apps: data_sources / data_apps / data_app_datasets / data_app_versions

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6
Create Date: 2026-07-25

新增「数据应用」相关表，支撑数据表格页面 / 可视化大屏的创建、预览与发布。
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d2e3f4a5b6c7"
down_revision: Union[str, Sequence[str], None] = "c1d2e3f4a5b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing = set(insp.get_table_names())

    if "data_sources" not in existing:
        op.create_table(
            "data_sources",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("kind", sa.String(length=50), nullable=False),
            sa.Column("dsn_secret_ref", sa.Text(), nullable=True),
            sa.Column("status", sa.String(length=50), nullable=False, server_default="untested"),
            sa.Column("tested_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
        )

    if "data_apps" not in existing:
        op.create_table(
            "data_apps",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("domain_id", sa.String(length=36), sa.ForeignKey("domain_contexts.id"), nullable=False),
            sa.Column("ontology_id", sa.String(length=36), nullable=True),
            sa.Column("app_type", sa.String(length=30), nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False, server_default="未命名应用"),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("owner", sa.String(length=255), nullable=True),
            sa.Column("status", sa.String(length=30), nullable=False, server_default="draft"),
            sa.Column("source", sa.String(length=30), nullable=False, server_default="manual"),
            sa.Column("spec_json", sa.Text(), nullable=True),
            sa.Column("current_version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("published_version", sa.Integer(), nullable=True),
            sa.Column("published_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
        )
        op.create_index("ix_data_apps_domain_id", "data_apps", ["domain_id"])

    if "data_app_datasets" not in existing:
        op.create_table(
            "data_app_datasets",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("app_id", sa.String(length=36), sa.ForeignKey("data_apps.id"), nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False, server_default="数据集"),
            sa.Column("primary_object_type_id", sa.String(length=36), nullable=True),
            sa.Column("binding_json", sa.Text(), nullable=True),
            sa.Column("compiled_sql", sa.Text(), nullable=True),
            sa.Column("data_source_id", sa.String(length=36), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
        )
        op.create_index("ix_data_app_datasets_app_id", "data_app_datasets", ["app_id"])

    if "data_app_versions" not in existing:
        op.create_table(
            "data_app_versions",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("app_id", sa.String(length=36), sa.ForeignKey("data_apps.id"), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("spec_snapshot_json", sa.Text(), nullable=True),
            sa.Column("datasets_snapshot_json", sa.Text(), nullable=True),
            sa.Column("diff_summary", sa.Text(), nullable=True),
            sa.Column("operator", sa.String(length=255), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        )
        op.create_index("ix_data_app_versions_app_id", "data_app_versions", ["app_id"])


def downgrade() -> None:
    for table in (
        "data_app_versions",
        "data_app_datasets",
        "data_apps",
        "data_sources",
    ):
        op.drop_table(table)
