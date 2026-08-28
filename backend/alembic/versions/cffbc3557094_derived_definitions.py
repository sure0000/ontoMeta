"""派生对象定义表：多表 join 出的新粒度实体，其上游/粒度/连接条件的声明。

同步与 1:1 清洗不产生新实体（一行代表的东西没变，那只是同一个实体的另一个落点）；
改变了粒度的宽表/汇总表才是新的业务概念，它需要在本体里有名字。这张表存的就是那份
**声明**——上游数据集引用、粒度、连接条件——而不是从物理表反推出来的猜测。

Revision ID: cffbc3557094
Revises: 623169d66806
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "cffbc3557094"
down_revision: Union[str, Sequence[str], None] = "623169d66806"
branch_labels = None
depends_on = None

_TABLE = "derived_definitions"


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if _TABLE in _tables():
        return
    op.create_table(
        _TABLE,
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("ontology_id", sa.String(length=36), sa.ForeignKey("ontologies.id"), nullable=False),
        sa.Column("object_type_id", sa.String(length=36), sa.ForeignKey("object_types.id"), nullable=False),
        sa.Column("grain", sa.Text(), nullable=False),
        sa.Column("upstream_refs_json", sa.Text(), nullable=False),
        sa.Column("joins_json", sa.Text(), nullable=True),
        sa.Column("field_mapping_json", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=True),
        sa.UniqueConstraint("object_type_id", name="uq_derived_definition_object"),
    )
    op.create_index("ix_derived_definitions_ontology_id", _TABLE, ["ontology_id"])
    op.create_index("ix_derived_definitions_object_type_id", _TABLE, ["object_type_id"])


def downgrade() -> None:
    if _TABLE in _tables():
        op.drop_index("ix_derived_definitions_object_type_id", table_name=_TABLE)
        op.drop_index("ix_derived_definitions_ontology_id", table_name=_TABLE)
        op.drop_table(_TABLE)
