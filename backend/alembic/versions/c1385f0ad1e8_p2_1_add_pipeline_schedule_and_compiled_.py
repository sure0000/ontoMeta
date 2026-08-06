"""P2-1: add pipeline schedule and compiled DAG tracking

Revision ID: c1385f0ad1e8
Revises: c2d3e4f5a6b7
Create Date: 2026-08-06 11:49:48.516471

只加 GovernanceTaskPipeline 的三个字段。autogenerate 误检的一堆无关约束/索引/NOT NULL
变更（既有 schema 漂移，非本次改动）已手动剔除——它们与 P2-1 无关，且 `drop_constraint(None)`
在 SQLite 上因约束无名直接报错。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c1385f0ad1e8'
down_revision: Union[str, Sequence[str], None] = 'c2d3e4f5a6b7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('governance_task_pipelines', schema=None) as batch_op:
        batch_op.add_column(sa.Column('schedule_cron', sa.String(length=120), nullable=True))
        batch_op.add_column(sa.Column('compiled_dag_id', sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column('compiled_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('governance_task_pipelines', schema=None) as batch_op:
        batch_op.drop_column('compiled_at')
        batch_op.drop_column('compiled_dag_id')
        batch_op.drop_column('schedule_cron')
