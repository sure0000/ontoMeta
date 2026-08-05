"""chat_bi_external_tools: 配置驱动的外部工具（P4 免改代码扩能力）

Revision ID: a3b4c5d6e7f8
Revises: f2a3b4c5d6e7
Create Date: 2026-08-05

运维注册外部 HTTP 工具（名称+描述+JSON-Schema 入参+端点+可选鉴权头），启用即注入 Data Agent
工具集（按域过滤+数量封顶），模型据描述自主调用，结果经通用 executor 封顶取回。
name 全局唯一；auth_header 机密不回显；domain_id 为空=全局。
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a3b4c5d6e7f8"
down_revision: Union[str, Sequence[str], None] = "f2a3b4c5d6e7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "chat_bi_external_tools" in set(insp.get_table_names()):
        return
    op.create_table(
        "chat_bi_external_tools",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("parameters_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("method", sa.String(length=10), nullable=False, server_default="POST"),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("auth_header", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("domain_id", sa.String(length=36), nullable=True),
        sa.Column("result_max_chars", sa.Integer(), nullable=False, server_default="4000"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
        sa.UniqueConstraint("name", name="uq_chat_bi_external_tools_name"),
    )
    op.create_index(
        "ix_chat_bi_external_tools_name", "chat_bi_external_tools", ["name"]
    )
    op.create_index(
        "ix_chat_bi_external_tools_domain_id", "chat_bi_external_tools", ["domain_id"]
    )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "chat_bi_external_tools" not in set(insp.get_table_names()):
        return
    op.drop_table("chat_bi_external_tools")
