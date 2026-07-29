"""rbac: principals 表（四层角色 + Token 哈希）

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-07-29

M0：管理端主体与四层角色（reader < editor < reviewer < publisher）。
Token 只存 SHA-256(pepper+明文) 与前缀，明文仅创建/轮换时返回一次。
ONTOMETA_ADMIN_TOKEN 保留为 superuser：未创建任何 principal 时行为与改造前一致。
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "principals"


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _TABLE in insp.get_table_names():
        return

    op.create_table(
        _TABLE,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("role", sa.String(30), nullable=False, server_default="reader"),
        sa.Column("token_hash", sa.String(128), nullable=False),
        sa.Column("token_prefix", sa.String(32), nullable=False, server_default=""),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_principals_token_hash", _TABLE, ["token_hash"])
    op.create_index("ix_principals_role", _TABLE, ["role"])


def downgrade() -> None:
    op.drop_index("ix_principals_role", table_name=_TABLE)
    op.drop_index("ix_principals_token_hash", table_name=_TABLE)
    op.drop_table(_TABLE)
