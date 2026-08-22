"""add dimensional_models

Revision ID: 2f7c9a1d4e6b
Revises: 6ac2622d9b62
Create Date: 2026-08-22

维度模型是建模工单确认后的显式设计制品；此前 ORM 已注册该表，但迁移链没有建表，
导致由 ``alembic upgrade head`` 创建的真实数据库缺少 ``dimensional_models``。
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import context, op


revision: str = "2f7c9a1d4e6b"
down_revision: Union[str, Sequence[str], None] = "6ac2622d9b62"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "dimensional_models"
_MODEL_TYPE = sa.Enum(
    "star",
    "snowflake",
    "constellation",
    name="dimensional_model_type",
)
_STATUS_TYPE = sa.Enum(
    "draft",
    "validated",
    "confirmed",
    "compiled",
    "deployed",
    name="dimensional_model_status",
)


def upgrade() -> None:
    """Create the dimensional-model design artifact table."""
    offline = context.is_offline_mode()
    bind = op.get_bind()
    inspector = None if offline else sa.inspect(bind)

    # Legacy/test databases may have run Base.metadata.create_all while their
    # Alembic revision stayed at the parent.  Do not fail on that mixed state.
    if offline or _TABLE not in set(inspector.get_table_names()):
        op.create_table(
            _TABLE,
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column(
                "modeling_case_id",
                sa.String(length=36),
                sa.ForeignKey("modeling_cases.id"),
                nullable=True,
            ),
            sa.Column(
                "domain_id",
                sa.String(length=36),
                sa.ForeignKey("domain_contexts.id"),
                nullable=False,
            ),
            sa.Column(
                "ontology_id",
                sa.String(length=36),
                sa.ForeignKey("ontologies.id"),
                nullable=False,
            ),
            sa.Column("name", sa.String(length=200), nullable=False),
            sa.Column("display_name", sa.String(length=200), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("business_process", sa.Text(), nullable=False),
            sa.Column("grain", sa.Text(), nullable=False),
            sa.Column("fact_tables", sa.JSON(), nullable=False),
            sa.Column("dimensions", sa.JSON(), nullable=False),
            sa.Column("conformed_dimensions", sa.JSON(), nullable=False),
            sa.Column("model_type", _MODEL_TYPE, nullable=False),
            sa.Column("validation_issues", sa.JSON(), nullable=True),
            sa.Column("compiled_contracts", sa.JSON(), nullable=True),
            sa.Column("compiled_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("status", _STATUS_TYPE, nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("created_by", sa.String(length=100), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        if not offline:
            inspector = sa.inspect(bind)

    existing_indexes = (
        set()
        if offline
        else {index["name"] for index in inspector.get_indexes(_TABLE)}
    )
    indexes = {
        "ix_dimensional_models_modeling_case_id": ["modeling_case_id"],
        "ix_dimensional_models_domain_id": ["domain_id"],
        "ix_dimensional_models_ontology_id": ["ontology_id"],
    }
    for name, columns in indexes.items():
        if name not in existing_indexes:
            op.create_index(name, _TABLE, columns, unique=False)


def downgrade() -> None:
    """Remove dimensional models and their PostgreSQL enum types."""
    op.drop_table(_TABLE)

    # PostgreSQL enum types are schema objects and survive DROP TABLE unless
    # explicitly removed.  On SQLite these calls are harmless no-ops.
    bind = op.get_bind()
    _STATUS_TYPE.drop(bind, checkfirst=True)
    _MODEL_TYPE.drop(bind, checkfirst=True)
