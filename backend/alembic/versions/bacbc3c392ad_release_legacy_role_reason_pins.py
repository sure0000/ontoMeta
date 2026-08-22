"""放开遗留的 role_reason 钉住

``5ec47c2fd4c3`` 把复核状态从 ``role_reason`` 的 ``[待复核]`` 前缀升格成独立列，但只
迁了值，没有清理它留下的**钉住**：旧的 ``_set_review_mark`` 直接改写 role_reason 文本，
于是每一次「确认复核」都会把 ``role_reason`` 写进 ``overridden_fields``——那不是用户想
钉住角色依据，只是确认动作的副作用。这些钉住会让机器永远刷新不了角色依据文本。

新代码里确认复核只改 ``needs_review`` 列、不碰 role_reason，也不再钉它。这里一次性把
遗留钉住放开，让 role_reason 回到「描述性字段、机器可持续刷新」的位置。

极少数用户确实手写过角色依据的对象会被一并放开（无法与确认副作用区分）；代价可控——
详情页的「人工权威字段」面板可以重新钉上。

Revision ID: bacbc3c392ad
Revises: 341f29e30b22
Create Date: 2026-08-21
"""

from __future__ import annotations

import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "bacbc3c392ad"
down_revision: Union[str, Sequence[str], None] = "341f29e30b22"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    rows = list(
        bind.execute(
            sa.text(
                "SELECT id, overridden_fields FROM object_types "
                "WHERE overridden_fields LIKE '%role_reason%'"
            )
        )
    )
    for row_id, raw in rows:
        try:
            fields = json.loads(raw) if raw else []
        except (TypeError, ValueError):
            continue
        if not isinstance(fields, list) or "role_reason" not in fields:
            continue
        remaining = [f for f in fields if f != "role_reason"]
        bind.execute(
            sa.text(
                "UPDATE object_types SET overridden_fields = :v WHERE id = :i"
            ),
            {
                "v": json.dumps(remaining, ensure_ascii=False) if remaining else None,
                "i": row_id,
            },
        )


def downgrade() -> None:
    # 放开是有意的一次性清理，不还原：还原会把机器重新锁在旧措辞上。
    pass
