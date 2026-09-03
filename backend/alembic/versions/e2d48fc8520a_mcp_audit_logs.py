"""mcp: mcp_audit_logs 表（工具调用审计，append-only）

Revision ID: e2d48fc8520a
Revises: e5004275d2b5
Create Date: 2026-09-03

Phase 3：MCP 工具调用审计。记录「谁、什么身份、调了哪个工具、成没成、是否被授权拦下」，
只追加不改写。principal_id 不设外键——匿名会话（未配 Token）该列为空，且 superuser
（ONTOMETA_ADMIN_TOKEN）也无对应 principals 行。
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e2d48fc8520a"
down_revision: Union[str, Sequence[str], None] = "e5004275d2b5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "mcp_audit_logs"


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _TABLE in insp.get_table_names():
        return

    op.create_table(
        _TABLE,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("principal_id", sa.String(36), nullable=True),
        sa.Column("principal_role", sa.String(30), nullable=True),
        sa.Column(
            "client_type", sa.String(30), nullable=False, server_default="mcp_local"
        ),
        sa.Column("tool_name", sa.String(100), nullable=False),
        sa.Column("arguments_json", sa.Text(), nullable=True),
        sa.Column("success", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("denied", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
    )
    op.create_index(
        f"ix_{_TABLE}_created_at", _TABLE, ["created_at"], unique=False
    )
    op.create_index(
        f"ix_{_TABLE}_principal_id", _TABLE, ["principal_id"], unique=False
    )
    op.create_index(f"ix_{_TABLE}_tool_name", _TABLE, ["tool_name"], unique=False)
    op.create_index(f"ix_{_TABLE}_success", _TABLE, ["success"], unique=False)
    op.create_index(f"ix_{_TABLE}_denied", _TABLE, ["denied"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _TABLE not in insp.get_table_names():
        return
    for idx in (
        f"ix_{_TABLE}_denied",
        f"ix_{_TABLE}_success",
        f"ix_{_TABLE}_tool_name",
        f"ix_{_TABLE}_principal_id",
        f"ix_{_TABLE}_created_at",
    ):
        op.drop_index(idx, table_name=_TABLE)
    op.drop_table(_TABLE)
