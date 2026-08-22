"""normalize schema drift left by legacy create_all/batch operations

Revision ID: c4e19b7a5d02
Revises: 8d31c6f0a2b4
Create Date: 2026-08-22

The canonical parent schema already matches ORM metadata.  Older databases can
nevertheless contain a mixed schema produced by ``Base.metadata.create_all`` or
an interrupted SQLite batch migration.  Repair only those detected anomalies;
a clean database is intentionally a no-op.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import context, op


revision: str = "c4e19b7a5d02"
down_revision: Union[str, Sequence[str], None] = "8d31c6f0a2b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_BINDING_CONSTRAINTS = {
    "business_logic_object_bindings": (
        "uq_logic_object_role",
        ("business_logic_id", "object_type_id", "role"),
    ),
    "business_logic_property_bindings": (
        "uq_logic_property_role",
        ("business_logic_id", "property_id", "role"),
    ),
}


def _repair_unique_constraints(bind: sa.engine.Connection) -> None:
    for table, (constraint_name, columns) in _BINDING_CONSTRAINTS.items():
        inspector = sa.inspect(bind)
        existing = {
            constraint["name"]
            for constraint in inspector.get_unique_constraints(table)
        }
        if constraint_name in existing:
            continue

        column_sql = ", ".join(columns)
        duplicate = bind.execute(
            sa.text(
                f"SELECT {column_sql}, COUNT(*) AS duplicate_count "
                f"FROM {table} GROUP BY {column_sql} HAVING COUNT(*) > 1 "
                "LIMIT 1"
            )
        ).first()
        if duplicate is not None:
            raise RuntimeError(
                f"{table} 存在重复业务键，无法安全添加 {constraint_name}："
                f"{tuple(duplicate)}。请先人工合并重复绑定后重跑迁移。"
            )

        with op.batch_alter_table(table) as batch_op:
            batch_op.create_unique_constraint(constraint_name, list(columns))


def _repair_business_logic_category_fk(bind: sa.engine.Connection) -> None:
    foreign_keys = sa.inspect(bind).get_foreign_keys("business_logics")
    category_fk = next(
        (
            fk
            for fk in foreign_keys
            if fk.get("constrained_columns") == ["category_id"]
        ),
        None,
    )
    if category_fk is None:
        with op.batch_alter_table("business_logics") as batch_op:
            batch_op.create_foreign_key(
                "fk_business_logics_category_id_business_logic_categories",
                "business_logic_categories",
                ["category_id"],
                ["id"],
                ondelete="SET NULL",
            )
        return

    ondelete = (category_fk.get("options") or {}).get("ondelete")
    if (ondelete or "").upper() == "SET NULL":
        return

    # SQLite reports unnamed foreign keys.  A naming convention gives the
    # reflected constraint a deterministic temporary name so batch mode can
    # replace it safely.
    convention = {
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"
    }
    constraint_name = (
        category_fk.get("name")
        or "fk_business_logics_category_id_business_logic_categories"
    )
    with op.batch_alter_table(
        "business_logics", naming_convention=convention
    ) as batch_op:
        batch_op.drop_constraint(constraint_name, type_="foreignkey")
        batch_op.create_foreign_key(
            "fk_business_logics_category_id_business_logic_categories",
            "business_logic_categories",
            ["category_id"],
            ["id"],
            ondelete="SET NULL",
        )


def _repair_conversation_schema(bind: sa.engine.Connection) -> None:
    inspector = sa.inspect(bind)
    columns = {
        column["name"]: column
        for column in inspector.get_columns("chat_bi_conversations")
    }
    nullable_flags = [
        column
        for column in ("is_pinned", "is_archived")
        if columns[column]["nullable"]
    ]
    if nullable_flags:
        bind.execute(
            sa.text(
                "UPDATE chat_bi_conversations "
                "SET is_pinned = COALESCE(is_pinned, 0), "
                "is_archived = COALESCE(is_archived, 0) "
                "WHERE is_pinned IS NULL OR is_archived IS NULL"
            )
        )
        with op.batch_alter_table("chat_bi_conversations") as batch_op:
            for column in nullable_flags:
                batch_op.alter_column(
                    column,
                    existing_type=sa.Boolean(),
                    nullable=False,
                )

    indexes = {
        index["name"]
        for index in sa.inspect(bind).get_indexes("chat_bi_conversations")
    }
    if "ix_chat_bi_conversations_category" in indexes:
        op.drop_index(
            "ix_chat_bi_conversations_category",
            table_name="chat_bi_conversations",
        )


def _drop_stale_batch_tables(bind: sa.engine.Connection) -> None:
    tables = set(sa.inspect(bind).get_table_names())
    for temporary in sorted(t for t in tables if t.startswith("_alembic_tmp_")):
        original = temporary.removeprefix("_alembic_tmp_")
        if original not in tables:
            raise RuntimeError(
                f"发现 Alembic 临时表 {temporary}，但原表 {original} 不存在；"
                "无法判断迁移中断阶段，请人工恢复后重跑。"
            )
        row_count = bind.execute(
            sa.text(f'SELECT COUNT(*) FROM "{temporary}"')
        ).scalar_one()
        if row_count:
            raise RuntimeError(
                f"发现含 {row_count} 行数据的 Alembic 临时表 {temporary}；"
                "为避免静默丢数据，迁移拒绝自动删除，请人工核对后重跑。"
            )
        op.drop_table(temporary)


def upgrade() -> None:
    """Repair only drift that is present in an online legacy database."""
    if context.is_offline_mode():
        # The canonical parent schema needs no DDL.  Legacy-state detection
        # requires live reflection and therefore cannot be represented safely
        # as unconditional offline SQL.
        return

    bind = op.get_bind()
    _drop_stale_batch_tables(bind)
    _repair_unique_constraints(bind)
    _repair_business_logic_category_fk(bind)
    _repair_conversation_schema(bind)


def downgrade() -> None:
    """No-op: the parent revision's canonical schema is already normalized."""
    pass
