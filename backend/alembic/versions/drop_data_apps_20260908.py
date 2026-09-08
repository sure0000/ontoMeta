"""删掉自研数据应用的四张表。

图表与看板改由 **Apache Superset** 承载：那边才是画图、存图、分享的地方，ontoMeta 只
提供口径（把已发布本体的落点登记成 Superset 数据集）与一张登记簿（``superset_assets``）。
自研那套（Dashboard/Panel 编辑器、口径编译、Mock 预览、发布快照、公开分享）随之整体下线。

**数据不迁移**。看板的布局与主题、发布版本快照、公开分享链接都是自研渲染器的产物，
在 Superset 里没有对应形态——照搬只会得到一堆打不开的东西。数据源与 Doris 数仓配置
（``data_sources`` / ``doris_warehouse_config``）**不在此列**，它们是物化、搬运、取数
共用的地基，原样保留。

Revision ID: drop_data_apps_20260908
Revises: superset_assets_20260908
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "drop_data_apps_20260908"
down_revision: str | Sequence[str] | None = "superset_assets_20260908"
branch_labels = None
depends_on = None

# 顺序有讲究：三张子表都带指向 data_apps.id 的外键，必须先删子表。
_TABLES = (
    "data_app_versions",
    "data_app_datasets",
    "data_app_widgets",
    "data_apps",
)


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    for table in _TABLES:
        if table in existing:
            op.drop_table(table)


def downgrade() -> None:
    """不重建。

    这四张表的建表 DDL 散在 d2e3f4a5b6c7（初建）、d8e9f0a1b2c3（tiles→panels）、
    e9f0a1b2c3d4（screen→canvas）三个修订里，在这里抄一份回来只会得到第二份定义；
    真要回退请降到本修订之前。**空实现是刻意的，不是遗漏。**
    """
