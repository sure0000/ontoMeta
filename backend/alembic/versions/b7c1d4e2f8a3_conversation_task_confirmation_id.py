"""chat_bi_conversation_tasks.confirmation_id：把闭环按任务分开

Revision ID: b7c1d4e2f8a3
Revises: d0f1a2b3c4d5
Create Date: 2026-08-30

确认闭环此前是**会话级**的：一条会话里的所有决策记录被并成一组六环。两个后果——
随口一问点个「认可」就点亮一环、于是一次纯查询也顶着一张闭环卡；一条会话建了三个
任务，三份确认混成一组六环，谁也说不清哪一环是给哪条任务走的。

闭环的正确粒度是**任务**。后三环（方案/执行/结果）本来就带 artifact 软引用，能归属；
前三环（需求/本体/数据）在制品还不存在时就确认了，只带表单向导的 task_confirmation_id。
本列把它落到 (会话, 制品) 关联上，前三环才有了归属依据。

历史行数据为空：那些任务的前三环无从归属，闭环里如实标灰——不猜。
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b7c1d4e2f8a3"
down_revision: Union[str, Sequence[str], None] = "d0f1a2b3c4d5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "chat_bi_conversation_tasks"
_COLUMN = "confirmation_id"
_INDEX = "ix_chat_bi_conversation_tasks_confirmation_id"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return
    if _COLUMN in {c["name"] for c in inspector.get_columns(_TABLE)}:
        return
    op.add_column(_TABLE, sa.Column(_COLUMN, sa.String(length=64), nullable=True))
    op.create_index(_INDEX, _TABLE, [_COLUMN])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return
    if _COLUMN not in {c["name"] for c in inspector.get_columns(_TABLE)}:
        return
    if _INDEX in {i["name"] for i in inspector.get_indexes(_TABLE)}:
        op.drop_index(_INDEX, table_name=_TABLE)
    op.drop_column(_TABLE, _COLUMN)
