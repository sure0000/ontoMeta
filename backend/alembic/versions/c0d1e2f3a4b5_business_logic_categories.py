"""B10: business_logic_categories table + expression_draft/expression_json columns

Revision ID: c0d1e2f3a4b5
Revises: b9a1b2c3d4e5
Create Date: 2026-07-21

NOTE: 本迁移原本创建 business_logic_categories 表并给 business_logics 增加
category_id / expression_draft / expression_json 三列。但这些 DDL 已全部包含在
基线迁移 5a881e5c0024 中,重复执行会在全新库上报 "table already exists"。
故此处改为空操作,仅保留 revision 以维持迁移链不断裂。
"""

from __future__ import annotations

from typing import Sequence, Union

revision: str = "c0d1e2f3a4b5"
down_revision: Union[str, Sequence[str], None] = "b9a1b2c3d4e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 空操作:相关 schema 已由基线迁移 5a881e5c0024 建立。
    pass


def downgrade() -> None:
    # 空操作:对应 upgrade 无实际变更。
    pass
