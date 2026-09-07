"""血缘补录：边区分 lineage / relation

Revision ID: c086109d5cb1
Revises: d7b5276879d6
Create Date: 2026-09-07

DDL 里声明的外键是**关联关系**不是血缘：「订单引用客户」不是「客户加工成订单」。
写进 DataHub 血缘图会被下游判成 derivation、命名成「派生出」。所以给边加 kind，
relation 的行不参与上报，只作为关联证据进本体。

存量行全部是血缘（DDL 外键此前根本没被解析），故默认 lineage。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "c086109d5cb1"
down_revision = "d7b5276879d6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "lineage_package_edges",
        sa.Column(
            "kind", sa.String(16), nullable=False, server_default="lineage"
        ),
    )
    op.create_index(
        "ix_lineage_package_edges_kind", "lineage_package_edges", ["kind"]
    )


def downgrade() -> None:
    op.drop_index("ix_lineage_package_edges_kind", "lineage_package_edges")
    op.drop_column("lineage_package_edges", "kind")
