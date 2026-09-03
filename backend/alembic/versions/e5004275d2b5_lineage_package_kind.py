"""补 lineage_packages.kind

上一条迁移里 kind 是后加的，而运行中的 dev 服务 reload 后由 ``create_all`` 抢先按
**当时的模型**建了表；建表迁移的 ``_has_table`` 守卫见表已存在便跳过，于是 kind 没
落到库上。这条把列补齐——判存在再加，重复执行无副作用。

（教训：``create_all`` 与迁移抢建表时，建表迁移的存在性守卫会把后加的列吞掉。）

Revision ID: e5004275d2b5
Revises: 9a4c60df0a2f
Create Date: 2026-09-03
"""

from alembic import op
import sqlalchemy as sa

revision = "e5004275d2b5"
down_revision = "9a4c60df0a2f"
branch_labels = None
depends_on = None

_TABLE = "lineage_packages"


def _columns() -> set[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(_TABLE)}


def upgrade() -> None:
    columns = _columns()
    if not columns or "kind" in columns:
        return
    op.add_column(
        _TABLE,
        sa.Column("kind", sa.String(length=16), nullable=False, server_default="scan"),
    )
    op.create_index("ix_lineage_packages_kind", _TABLE, ["kind"], unique=False)


def downgrade() -> None:
    if "kind" in _columns():
        op.drop_index("ix_lineage_packages_kind", table_name=_TABLE)
        op.drop_column(_TABLE, "kind")
