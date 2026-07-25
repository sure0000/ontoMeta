"""ontology provenance: field-level origin, machine baseline, three-way merge metadata

为 ObjectType / Property / RelationType / BusinessLogic 增加字段级溯源与三方合并元数据，
为 RelationType 增加稳定身份键 source_signature，为 Ontology 增加 draft_revision，
为 DraftGenerationTask 增加 merge_report_json。

回填策略（见 ONTOLOGY_VERSIONING_PLAN.md §9）：
- machine_baseline = 当前可合并字段值（首次再生成不产生虚假冲突）
- status==suggested → origin=machine（机器可自由接管）
- 其余（edited/approved/pre_published/published）→ origin=machine_edited，
  overridden_fields=全部可合并字段（保守保护存量人工成果）

Revision ID: c1d2e3f4a5b6
Revises: a4b5c6d7e8f9
Create Date: 2026-07-25
"""

from __future__ import annotations

import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c1d2e3f4a5b6"
down_revision: Union[str, Sequence[str], None] = "a4b5c6d7e8f9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# 各实体的可合并字段（与 services/ontology_merge.py 保持一致）
MERGEABLE_FIELDS = {
    "object_types": ["name", "display_name", "description", "table_role", "role_reason"],
    "properties": ["display_name", "description", "data_type", "semantic_type"],
    "relation_types": ["display_name", "description", "cardinality", "structure_type"],
    "business_logics": ["display_name", "description", "expression_summary", "logic_type"],
}

_PROVENANCE_TABLES = list(MERGEABLE_FIELDS.keys())


def _add_provenance_columns(table: str) -> None:
    with op.batch_alter_table(table, schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("origin", sa.String(length=30), nullable=False, server_default="machine")
        )
        batch_op.add_column(sa.Column("overridden_fields", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("machine_baseline", sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column("user_created", sa.Boolean(), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column("deleted_by_user", sa.Boolean(), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column("upstream_removed", sa.Boolean(), nullable=False, server_default="0")
        )
        batch_op.add_column(sa.Column("last_generation_id", sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column("conflict_json", sa.Text(), nullable=True))


def _backfill(table: str) -> None:
    bind = op.get_bind()
    fields = MERGEABLE_FIELDS[table]
    col_list = ", ".join(["id", "status", *fields])
    rows = bind.execute(sa.text(f"SELECT {col_list} FROM {table}")).mappings().all()
    for row in rows:
        baseline = {f: row[f] for f in fields}
        if row["status"] == "suggested":
            origin = "machine"
            overridden = None
        else:
            origin = "machine_edited"
            overridden = json.dumps(fields, ensure_ascii=False)
        bind.execute(
            sa.text(
                f"UPDATE {table} SET machine_baseline = :baseline, "
                f"origin = :origin, overridden_fields = :overridden WHERE id = :id"
            ),
            {
                "baseline": json.dumps(baseline, ensure_ascii=False),
                "origin": origin,
                "overridden": overridden,
                "id": row["id"],
            },
        )


def upgrade() -> None:
    for table in _PROVENANCE_TABLES:
        _add_provenance_columns(table)

    # RelationType 稳定身份键
    with op.batch_alter_table("relation_types", schema=None) as batch_op:
        batch_op.add_column(sa.Column("source_signature", sa.String(length=512), nullable=True))
        batch_op.create_index(
            "ix_relation_types_source_signature", ["source_signature"], unique=False
        )

    # Ontology 草稿演进计数
    with op.batch_alter_table("ontologies", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("draft_revision", sa.Integer(), nullable=False, server_default="0")
        )

    # 生成运行合并报告
    with op.batch_alter_table("draft_generation_tasks", schema=None) as batch_op:
        batch_op.add_column(sa.Column("merge_report_json", sa.Text(), nullable=True))

    for table in _PROVENANCE_TABLES:
        _backfill(table)


def downgrade() -> None:
    with op.batch_alter_table("draft_generation_tasks", schema=None) as batch_op:
        batch_op.drop_column("merge_report_json")

    with op.batch_alter_table("ontologies", schema=None) as batch_op:
        batch_op.drop_column("draft_revision")

    with op.batch_alter_table("relation_types", schema=None) as batch_op:
        batch_op.drop_index("ix_relation_types_source_signature")
        batch_op.drop_column("source_signature")

    for table in _PROVENANCE_TABLES:
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_column("conflict_json")
            batch_op.drop_column("last_generation_id")
            batch_op.drop_column("upstream_removed")
            batch_op.drop_column("deleted_by_user")
            batch_op.drop_column("user_created")
            batch_op.drop_column("machine_baseline")
            batch_op.drop_column("overridden_fields")
            batch_op.drop_column("origin")
