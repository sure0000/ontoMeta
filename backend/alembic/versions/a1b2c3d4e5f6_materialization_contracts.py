"""materialization contracts: 本体实体 → 物理落地配置

Revision ID: a1b2c3d4e5f6
Revises: f0a1b2c3d4e5
Create Date: 2026-07-29

M1：物化契约。本体是一级源数据、物理表是二级投影，但「投影到哪一层、怎么增量、
按什么分区、是否留历史」本体不承载——由本表补齐。携带完整溯源字段参与三方合并，
机器重新推导不得覆盖人工钉住的字段。
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "f0a1b2c3d4e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "materialization_contracts"


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _TABLE in insp.get_table_names():
        return

    op.create_table(
        _TABLE,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "ontology_id",
            sa.String(36),
            sa.ForeignKey("ontologies.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("target_kind", sa.String(50), nullable=False, index=True),
        sa.Column("target_id", sa.String(36), nullable=False, index=True),
        sa.Column(
            "target_layer", sa.String(20), nullable=False, server_default="dim"
        ),
        sa.Column("target_engines", sa.Text(), nullable=True),
        sa.Column(
            "load_strategy", sa.String(20), nullable=False, server_default="full"
        ),
        sa.Column("partition_key", sa.String(255), nullable=True),
        sa.Column("scd_type", sa.String(20), nullable=False, server_default="none"),
        sa.Column("refresh_cron", sa.String(100), nullable=True),
        sa.Column(
            "materialized", sa.Boolean(), nullable=False, server_default="1"
        ),
        sa.Column("derivation_reason", sa.Text(), nullable=True),
        # 溯源与三方合并
        sa.Column("origin", sa.String(30), nullable=False, server_default="machine"),
        sa.Column("overridden_fields", sa.Text(), nullable=True),
        sa.Column("machine_baseline", sa.Text(), nullable=True),
        sa.Column("user_created", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("deleted_by_user", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("upstream_removed", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("last_generation_id", sa.String(36), nullable=True),
        sa.Column("conflict_json", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "ontology_id",
            "target_kind",
            "target_id",
            name="uq_materialization_contract_target",
        ),
    )


def downgrade() -> None:
    op.drop_table(_TABLE)
