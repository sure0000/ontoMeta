"""object_types / properties / relation_types: has_unpublished_change

见 ``docs/ONTOLOGY_LIFECYCLE_REDESIGN.md`` §3.4（A 案）。人工编辑已发布实体后不再把它
退回 ``edited``——改动立即对外生效，人是最高权威。代价是「已发布内容被改过、还没打成
新版本」这件事在库里失去了痕迹：靠 ``updated_at`` 与 ``ontologies.published_at`` 比时间
戳做不到，SQLite 的 ``CURRENT_TIMESTAMP`` 只有秒级精度，同秒内的发布与编辑分辨不出来。

这一列就是「待固化」的唯一凭据：人工编辑已发布实体时置位，``publish()`` 提升实体时清零。
页头的「N 项待固化」提示条与域卡片指标都读它。

Revision ID: 341f29e30b22
Revises: 5ec47c2fd4c3
Create Date: 2026-08-21
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "341f29e30b22"
down_revision: Union[str, Sequence[str], None] = "5ec47c2fd4c3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = ("object_types", "properties", "relation_types")


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    for table in _TABLES:
        columns = {c["name"] for c in inspector.get_columns(table)}
        if "has_unpublished_change" in columns:
            continue
        op.add_column(
            table,
            sa.Column(
                "has_unpublished_change",
                sa.Boolean(),
                nullable=False,
                server_default="0",
            ),
        )


def downgrade() -> None:
    for table in _TABLES:
        op.drop_column(table, "has_unpublished_change")
