"""依赖组件只连已有服务：去掉部署方式/部署日志，deploy_* 改名为连接语义

Revision ID: 61746a99b7fa
Revises: db3055d82d31
Create Date: 2026-09-08

ontoMeta 不再部署任何依赖（docker/k8s/物理机三条路径已删），组件固定为
LLM / DataHub / Airflow，只登记连接信息 + 拨测。表里因此没有「部署」这回事：

- 删 ``deploy_mode``（恒 external）、``deploy_log``（部署命令日志）
- ``deploy_spec_json`` → ``settings_json``（现在只放 Airflow 编排参数与拨测记账）
- ``deploy_status`` → ``connection_status``，``deploy_error`` → ``connection_error``
- 状态值收敛为 unknown/connected/failed：not_deployed/deploying/deployed 一律回
  ``unknown``（"装完了"不等于"连得上"，重新拨测才有结论）
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "61746a99b7fa"
down_revision = "db3055d82d31"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("dependency_components") as batch:
        batch.alter_column("deploy_spec_json", new_column_name="settings_json")
        batch.alter_column(
            "deploy_status",
            new_column_name="connection_status",
            existing_type=sa.String(16),
            server_default=None,
        )
        batch.alter_column("deploy_error", new_column_name="connection_error")
        batch.drop_column("deploy_mode")
        batch.drop_column("deploy_log")
    op.execute(
        "UPDATE dependency_components SET connection_status = 'unknown' "
        "WHERE connection_status NOT IN ('connected', 'failed')"
    )


def downgrade() -> None:
    with op.batch_alter_table("dependency_components") as batch:
        batch.alter_column("settings_json", new_column_name="deploy_spec_json")
        batch.alter_column(
            "connection_status",
            new_column_name="deploy_status",
            existing_type=sa.String(16),
            server_default=None,
        )
        batch.alter_column("connection_error", new_column_name="deploy_error")
        batch.add_column(
            sa.Column(
                "deploy_mode", sa.String(16), nullable=False, server_default="external"
            )
        )
        batch.add_column(sa.Column("deploy_log", sa.Text(), nullable=True))
    op.execute(
        "UPDATE dependency_components SET deploy_status = 'not_deployed' "
        "WHERE deploy_status = 'unknown'"
    )
