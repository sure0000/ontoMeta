"""制品记「谁建的、从哪个入口建的」。

按会话组织的决策账本随 Data Agent 会话一起退场后，治理制品成为「机器提了什么、人定了
什么」的唯一记录。既然唯一，创建时刻的身份就不能缺：``origin`` 只分得出 machine/user，
分不出是谁、是 Web 还是外部 agent 经 MCP 建的。

``overridden_fields`` 列早已存在（建表时随溯源一组加的）但从没人写，本次由
``agent_pipeline.edit()`` 开始落值，无需 DDL。

Revision ID: artifact_provenance_20260905
Revises: cb_msg_preview_idx_20260905
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "artifact_provenance_20260905"
down_revision: str | Sequence[str] | None = "cb_msg_preview_idx_20260905"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    bind = op.get_bind()
    return {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    # 存量库可能已被 create_all 建出这两列（本仓测试与本地起服务都走 create_all），
    # 故逐列判断——重复 ADD COLUMN 在 SQLite 上是硬错。
    existing = _columns("governance_artifacts")
    if "created_by" not in existing:
        op.add_column(
            "governance_artifacts",
            sa.Column("created_by", sa.String(length=255), nullable=True),
        )
    if "created_via" not in existing:
        op.add_column(
            "governance_artifacts",
            sa.Column("created_via", sa.String(length=30), nullable=True),
        )
        op.create_index(
            "ix_governance_artifacts_created_via",
            "governance_artifacts",
            ["created_via"],
        )


def downgrade() -> None:
    existing = _columns("governance_artifacts")
    if "created_via" in existing:
        op.drop_index(
            "ix_governance_artifacts_created_via", table_name="governance_artifacts"
        )
        op.drop_column("governance_artifacts", "created_via")
    if "created_by" in existing:
        op.drop_column("governance_artifacts", "created_by")
