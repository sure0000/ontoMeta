"""governance_standard_records: 治理规约的发布记录（G3 建表补迁移）

Revision ID: b1c2d3e4f5a6
Revises: f2a3b4c5d6e7
Create Date: 2026-08-05

``GovernanceStandardRecord`` 模型随 G3 加入，但**当时漏了迁移**：测试库靠
``Base.metadata.create_all`` 建表所以一直是绿的，而按 alembic 升级上来的库里根本没有这张表。
后果是 ``active_standard(db)`` 一查就 OperationalError——Data Agent 的 lint_against_standard
在真实环境里直接报「工具异常」，命名规约自检形同虚设。这里补上。

建表即可，不塞初始行：``services/governance_standard`` 在库里查不到 published 记录时回落到
代码里的默认规约常量，空表是合法状态。
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b1c2d3e4f5a6"
down_revision: Union[str, Sequence[str], None] = "f2a3b4c5d6e7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "governance_standard_records" in set(insp.get_table_names()):
        return
    op.create_table(
        "governance_standard_records",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("version", sa.String(length=50), nullable=False),
        # draft（拟发布）| published（当前生效）| superseded（被后来的发布顶替）
        sa.Column("status", sa.String(length=20), nullable=False, server_default="published"),
        # 发布那一刻规约的只读快照，仅供审计/diff；运行时按 version 回代码注册表取
        sa.Column("payload_json", sa.Text(), nullable=True),
        sa.Column("note", sa.String(length=512), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("activated_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_governance_standard_records_status", "governance_standard_records", ["status"]
    )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "governance_standard_records" not in set(insp.get_table_names()):
        return
    op.drop_table("governance_standard_records")
