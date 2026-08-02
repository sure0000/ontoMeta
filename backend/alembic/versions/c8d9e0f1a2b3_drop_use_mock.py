"""drop use_mock columns: 去除所有 mock 开关（datahub/cube/llm 服务）

「不需要 mock 功能」：连接不再有 mock 回退，一律走真实服务，未配置即显式报错。
DatahubSetting / CubeSetting / LlmServiceConfig 三张表各去掉 use_mock 列。

Revision ID: c8d9e0f1a2b3
Revises: b7c8d9e0f1a2
Create Date: 2026-08-02
"""

from typing import Sequence, Union

from alembic import op

revision: str = "c8d9e0f1a2b3"
down_revision: Union[str, Sequence[str], None] = "b7c8d9e0f1a2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    for table in ("datahub_settings", "cube_settings", "llm_service_configs"):
        with op.batch_alter_table(table) as batch:
            batch.drop_column("use_mock")


def downgrade() -> None:
    import sqlalchemy as sa

    for table, default in (
        ("datahub_settings", "0"),
        ("cube_settings", "1"),
        ("llm_service_configs", "0"),
    ):
        with op.batch_alter_table(table) as batch:
            batch.add_column(
                sa.Column(
                    "use_mock",
                    sa.Boolean(),
                    nullable=False,
                    server_default=default,
                )
            )
