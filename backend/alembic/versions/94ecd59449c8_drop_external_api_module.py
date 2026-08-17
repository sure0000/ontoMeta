"""drop external api module tables

外部 API 模块（对外只读 REST v1 + MCP 服务 + App Key 管理）整体移除，其两张表
``external_apps`` / ``external_api_call_logs`` 随之下线。

``downgrade`` 按 baseline(5a881e5c0024) + B8(b8e0a1c2d3f4) 的最终形态重建两表，
保证 upgrade/downgrade 可往返；建表后不还原数据（Key 只存哈希，本就不可复原，
应用移除后也无处使用）。

Revision ID: 94ecd59449c8
Revises: a1b2c3d4e5f7
Create Date: 2026-08-13
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "94ecd59449c8"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _existing_tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    tables = _existing_tables()
    # 子表先行：external_api_call_logs.app_id 外键指向 external_apps
    if "external_api_call_logs" in tables:
        op.drop_table("external_api_call_logs")
    if "external_apps" in tables:
        op.drop_table("external_apps")


def downgrade() -> None:
    tables = _existing_tables()
    if "external_apps" not in tables:
        op.create_table(
            "external_apps",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("app_key", sa.String(length=64), nullable=False),
            sa.Column("api_key_hash", sa.String(length=64), nullable=True),
            sa.Column("api_key_prefix", sa.String(length=16), nullable=True),
            sa.Column("api_key", sa.String(length=128), nullable=True),
            sa.Column("scopes", sa.Text(), nullable=True),
            sa.Column("rate_limit_per_minute", sa.Integer(), nullable=True),
            sa.Column("status", sa.String(length=50), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(),
                server_default=sa.text("(CURRENT_TIMESTAMP)"),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(),
                server_default=sa.text("(CURRENT_TIMESTAMP)"),
                nullable=False,
            ),
            sa.Column("last_used_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        with op.batch_alter_table("external_apps", schema=None) as batch_op:
            batch_op.create_index(
                batch_op.f("ix_external_apps_api_key"), ["api_key"], unique=True
            )
            batch_op.create_index(
                batch_op.f("ix_external_apps_api_key_hash"), ["api_key_hash"], unique=True
            )
            batch_op.create_index(
                batch_op.f("ix_external_apps_api_key_prefix"), ["api_key_prefix"], unique=False
            )
            batch_op.create_index(
                batch_op.f("ix_external_apps_app_key"), ["app_key"], unique=True
            )
            batch_op.create_index(
                batch_op.f("ix_external_apps_status"), ["status"], unique=False
            )

    if "external_api_call_logs" not in tables:
        op.create_table(
            "external_api_call_logs",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("app_id", sa.String(length=36), nullable=False),
            sa.Column("tool_name", sa.String(length=128), nullable=True),
            sa.Column("path", sa.String(length=255), nullable=True),
            sa.Column("status_code", sa.Integer(), nullable=False),
            sa.Column("duration_ms", sa.Integer(), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(),
                server_default=sa.text("(CURRENT_TIMESTAMP)"),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(["app_id"], ["external_apps.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        with op.batch_alter_table("external_api_call_logs", schema=None) as batch_op:
            batch_op.create_index(
                batch_op.f("ix_external_api_call_logs_app_id"), ["app_id"], unique=False
            )
            batch_op.create_index(
                batch_op.f("ix_external_api_call_logs_tool_name"), ["tool_name"], unique=False
            )
            batch_op.create_index(
                batch_op.f("ix_external_api_call_logs_created_at"), ["created_at"], unique=False
            )
