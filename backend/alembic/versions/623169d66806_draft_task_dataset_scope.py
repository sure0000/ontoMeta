"""按表裁剪的生成范围：草稿任务记下这次只处理哪些 DataHub 数据集。

全域重扫是「重新生成产生大量重复」与「一次几十万 token」的根源。真正需要建模的
往往只是源库新加的几张表，故生成任务要能带一份数据集清单，只对它们跑 LLM。

清单必须落库而不是留在进程内：草稿生成跑在独立子进程（``app.jobs.draft_worker``），
worker 只拿 task_id 回查这一行，参数进不去内存。为空/NULL＝全域，行为与历史一致。

Revision ID: 623169d66806
Revises: e4f5a6b7c8d9
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "623169d66806"
down_revision: Union[str, Sequence[str], None] = "e4f5a6b7c8d9"
branch_labels = None
depends_on = None

_TABLE = "draft_generation_tasks"


def _columns() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if _TABLE not in set(inspector.get_table_names()):
        return set()
    return {c["name"] for c in inspector.get_columns(_TABLE)}


def upgrade() -> None:
    if "dataset_urns_json" not in _columns():
        op.add_column(_TABLE, sa.Column("dataset_urns_json", sa.Text(), nullable=True))


def downgrade() -> None:
    if "dataset_urns_json" in _columns():
        op.drop_column(_TABLE, "dataset_urns_json")
