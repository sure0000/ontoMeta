"""object_types.needs_review：复核状态升格为独立列

见 ``docs/ONTOLOGY_LIFECYCLE_REDESIGN.md``。此前复核状态是 ``role_reason`` 的
``[待复核]`` 前缀，而 ``role_reason`` 同时是三方合并的可合并字段——两件事耦在一个
字符串里，代价是：

- 人工确认（去掉前缀）会让该字段与基线不同 → 被永久钉住 → 机器再也刷新不了角色依据；
- 机器换个措辞即构成双改 → 冲突面板冒出一条「角色依据」→ 点「采纳上游」把前缀写回
  → 该对象被**静默重新打成待复核**、下次发布直接掉出发布集；
- ``resolve-all + accept_theirs`` 等于一键作废全域人工复核。

拆列后 ``role_reason`` 回归纯描述文本（描述性字段，机器可持续刷新），复核状态只由
人改。回填：带前缀者置 True，并把前缀从文本里剥掉。

Revision ID: 5ec47c2fd4c3
Revises: 4aa435f23621
Create Date: 2026-08-21
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "5ec47c2fd4c3"
down_revision: Union[str, Sequence[str], None] = "4aa435f23621"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_MARK = "[待复核]"


def upgrade() -> None:
    bind = op.get_bind()
    columns = {c["name"] for c in sa.inspect(bind).get_columns("object_types")}
    if "needs_review" not in columns:
        op.add_column(
            "object_types",
            sa.Column(
                "needs_review",
                sa.Boolean(),
                nullable=False,
                server_default="0",
            ),
        )
        op.create_index(
            "ix_object_types_needs_review", "object_types", ["needs_review"]
        )

    bind.execute(
        sa.text(
            "UPDATE object_types SET needs_review = true "
            "WHERE role_reason LIKE :pat"
        ),
        {"pat": f"%{_MARK}%"},
    )
    # 剥前缀：标记只出现在开头，剥完再去掉残留的前导空白。
    bind.execute(
        sa.text(
            "UPDATE object_types "
            "SET role_reason = TRIM(REPLACE(role_reason, :mark, '')) "
            "WHERE role_reason LIKE :pat"
        ),
        {"mark": _MARK, "pat": f"%{_MARK}%"},
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "UPDATE object_types "
            "SET role_reason = :mark || ' ' || COALESCE(role_reason, '') "
            "WHERE needs_review = true"
        ),
        {"mark": _MARK},
    )
    op.drop_index("ix_object_types_needs_review", table_name="object_types")
    op.drop_column("object_types", "needs_review")
