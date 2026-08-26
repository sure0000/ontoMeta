"""Doris BE HTTP 地址（benodes）：Flink 连接器写 Doris 时不必再问 FE 要 BE 地址。

单机/容器化的 Doris 里 BE 在 FE 注册的 Host 常是 127.0.0.1（``show backends``），
集群外的 Flink 拿到它只会连自己：``Connect to 127.0.0.1:8040 failed``，且这个错
出现在作业运行期（TaskManager），提交回执里只看得到 "Failed to wait job finish"。
配了 benodes 就直接下发给连接器，绕开 FE 的那份地址。

Revision ID: e4f5a6b7c8d9
Revises: d3e4f5a6b7c8
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e4f5a6b7c8d9"
down_revision: Union[str, Sequence[str], None] = "d3e4f5a6b7c8"
branch_labels = None
depends_on = None

_TABLE = "doris_warehouse_configs"


def _columns() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if _TABLE not in set(inspector.get_table_names()):
        return set()
    return {c["name"] for c in inspector.get_columns(_TABLE)}


def upgrade() -> None:
    if "benodes_json" not in _columns():
        op.add_column(_TABLE, sa.Column("benodes_json", sa.Text(), nullable=True))


def downgrade() -> None:
    if "benodes_json" in _columns():
        op.drop_column(_TABLE, "benodes_json")
