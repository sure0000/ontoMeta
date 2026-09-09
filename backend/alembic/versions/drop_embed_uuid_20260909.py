"""drop superset_assets.embedded_uuid：内嵌预览整体移除

Revision ID: drop_embed_uuid_20260909  （id 要短：alembic_version.version_num 是 varchar(32)）
Revises: superset_database_id_20260909
Create Date: 2026-09-09

图表与看板一律走跳转链接在 Superset 里打开，ontoMeta 不再内嵌、不再签 guest token，
这一列（``embedDashboard`` 的 id）随之无人读写。

要回退到内嵌：``git revert`` 那次提交把代码拿回来，再 ``alembic downgrade`` 把列加回来。
列里原先存的 uuid 不保留——它只对某一个 Superset 实例有意义，重新 ``enable_embedded``
一次即可再拿到。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "drop_embed_uuid_20260909"
down_revision: str | None = "superset_database_id_20260909"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 存在性守卫：测试库由 Base.metadata.create_all 按当前模型建，模型里已经没有这一列，
    # 无条件 drop 会在全新库上炸。
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("superset_assets")}
    if "embedded_uuid" in cols:
        op.drop_column("superset_assets", "embedded_uuid")


def downgrade() -> None:
    op.add_column(
        "superset_assets",
        sa.Column("embedded_uuid", sa.String(length=64), nullable=True),
    )
