"""data app widgets: reusable chart/table assets

Revision ID: a5b6c7d8e9f0
Revises: f4a5b6c7d8e9
Create Date: 2026-07-25

D2：图表成为可复用资产，可被多个看板引用。
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a5b6c7d8e9f0"
down_revision: Union[str, Sequence[str], None] = "f4a5b6c7d8e9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "data_app_widgets" not in set(insp.get_table_names()):
        op.create_table(
            "data_app_widgets",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("domain_id", sa.String(length=36), sa.ForeignKey("domain_contexts.id"), nullable=False),
            sa.Column("ontology_id", sa.String(length=36), nullable=True),
            sa.Column("name", sa.String(length=255), nullable=False, server_default="未命名图表"),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("widget_type", sa.String(length=30), nullable=False, server_default="table"),
            sa.Column("primary_object_type_id", sa.String(length=36), nullable=True),
            sa.Column("binding_json", sa.Text(), nullable=True),
            sa.Column("viz_json", sa.Text(), nullable=True),
            sa.Column("compiled_sql", sa.Text(), nullable=True),
            sa.Column("data_source_id", sa.String(length=36), nullable=True),
            sa.Column("status", sa.String(length=30), nullable=False, server_default="draft"),
            sa.Column("source", sa.String(length=30), nullable=False, server_default="manual"),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
        )
        op.create_index("ix_data_app_widgets_domain_id", "data_app_widgets", ["domain_id"])


def downgrade() -> None:
    op.drop_table("data_app_widgets")
