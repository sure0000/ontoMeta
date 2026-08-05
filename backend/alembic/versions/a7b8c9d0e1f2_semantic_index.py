"""semantic_index_entries: 已发布实体的嵌入向量（P1.5 语义检索）

Revision ID: a7b8c9d0e1f2
Revises: c6d7e8f9a0b1
Create Date: 2026-08-05

Data Agent 的 search_* 原本纯 ILIKE，中文同义词（客户 / 往来单位 / Customer）一个都命不中，
只能在 prompt 里写「关键词优先用中文」这种补丁。本表存已发布对象与业务逻辑的嵌入向量，
供混合检索（ILIKE 精确优先 + 向量补召回）。

**不用 pgvector**：它要装 Postgres 扩展，而测试跑 SQLite；本项目一个域的可检索实体是
百到千级，暴力余弦足够快。向量以 JSON 文本存储，两种数据库通用。
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a7b8c9d0e1f2"
down_revision: Union[str, Sequence[str], None] = "c6d7e8f9a0b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "semantic_index_entries" in set(insp.get_table_names()):
        return
    op.create_table(
        "semantic_index_entries",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("ontology_id", sa.String(length=36), nullable=False),
        sa.Column("ontology_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("kind", sa.String(length=50), nullable=False),
        sa.Column("entity_id", sa.String(length=36), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("vector_json", sa.Text(), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False, server_default=""),
        sa.Column("dim", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["ontology_id"], ["ontologies.id"]),
    )
    op.create_index(
        "ix_semantic_index_entries_ontology_id",
        "semantic_index_entries",
        ["ontology_id"],
    )
    op.create_index(
        "ix_semantic_index_entries_ontology_version",
        "semantic_index_entries",
        ["ontology_version"],
    )
    op.create_index(
        "ix_semantic_index_entries_kind", "semantic_index_entries", ["kind"]
    )
    op.create_index(
        "ix_semantic_index_entries_entity_id", "semantic_index_entries", ["entity_id"]
    )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "semantic_index_entries" not in set(insp.get_table_names()):
        return
    op.drop_table("semantic_index_entries")
