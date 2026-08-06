"""add profiling columns to Property and ObjectType

DataHub 已提供的 profiling 元数据（sample_values / unique_count / row_count）
此前只在建模瞬间喂给 LLM 后即丢弃。现落库到本地，供阶梯式加载等场景直接读取，
减少对源库的真实数据查询（column_profiler SQL 现算作为兜底）。

Revision ID: 821bc8acf9c4
Revises: d467f452d8b8
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "821bc8acf9c4"
down_revision = "d467f452d8b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Property 字段级 profiling
    op.add_column(
        "properties",
        sa.Column("sample_values_json", sa.Text, nullable=True),
    )
    op.add_column(
        "properties",
        sa.Column("unique_count", sa.Integer, nullable=True),
    )
    # ObjectType 对象级总行数
    op.add_column(
        "object_types",
        sa.Column("row_count", sa.Integer, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("properties", "sample_values_json")
    op.drop_column("properties", "unique_count")
    op.drop_column("object_types", "row_count")
