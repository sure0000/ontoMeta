"""P3-2: add depends_on_json to pipeline steps for DAG topology

Revision ID: d467f452d8b8
Revises: c1385f0ad1e8
Create Date: 2026-08-06 14:02:02.088104

P3-2 让链支持扇出/汇聚（DAG 形态）——步骤可显式声明依赖的上游步骤索引列表。
空/None = 沿用线性默认（依赖上一步）。autogenerate 误检的一堆无关约束/索引/NOT NULL
变更已手动剔除。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd467f452d8b8'
down_revision: Union[str, Sequence[str], None] = 'c1385f0ad1e8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('governance_task_pipeline_steps', schema=None) as batch_op:
        batch_op.add_column(sa.Column('depends_on_json', sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('governance_task_pipeline_steps', schema=None) as batch_op:
        batch_op.drop_column('depends_on_json')
