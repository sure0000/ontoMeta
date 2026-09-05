"""governance_artifacts：代执行授权（与角色正交的第二道闸）

Revision ID: mcp_agent_exec_approval_20260904
Revises: mcp_skill_versions_20260904
Create Date: 2026-09-04

角色回答的是「这个身份能不能做这类事」，一发就长期有效；「这一条任务现在可以让
外部 agent 自己确认并推到远端」是另一个决定，得逐条给。这三列就是后者。

只有 REST（人在界面上点）能写；MCP 工具一律只读它。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "mcp_agent_exec_approval_20260904"
down_revision = "mcp_skill_versions_20260904"
branch_labels = None
depends_on = None

_TABLE = "governance_artifacts"


def _columns() -> set[str]:
    bind = op.get_bind()
    return {c["name"] for c in sa.inspect(bind).get_columns(_TABLE)}


def upgrade() -> None:
    existing = _columns()
    if "agent_execution_approved" not in existing:
        op.add_column(
            _TABLE,
            # server_default 与模型一致用 "0"：SQLite 与 PostgreSQL 都吃这个字面量，
            # 仓里既有的布尔列（needs_review 等）也是这么加的。
            sa.Column(
                "agent_execution_approved",
                sa.Boolean(),
                nullable=False,
                server_default="0",
            ),
        )
    if "agent_execution_approved_by" not in existing:
        op.add_column(
            _TABLE, sa.Column("agent_execution_approved_by", sa.String(255), nullable=True)
        )
    if "agent_execution_approved_at" not in existing:
        op.add_column(
            _TABLE, sa.Column("agent_execution_approved_at", sa.DateTime(), nullable=True)
        )


def downgrade() -> None:
    existing = _columns()
    for name in (
        "agent_execution_approved_at",
        "agent_execution_approved_by",
        "agent_execution_approved",
    ):
        if name in existing:
            op.drop_column(_TABLE, name)
