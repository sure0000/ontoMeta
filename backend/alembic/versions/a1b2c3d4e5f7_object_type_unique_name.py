"""object_types: unique (ontology_id, name)

对象标识名在本体内必须唯一。``name`` 是 Agent 写 SQL 用的标识符，也是
``ontology_projection`` 按归一小写建索引的键——重名会让 ``object_of`` 静默只解析到
其中一个，SQL 打到哪张表全看入库顺序。此前只在发布期由 ``validate_ontology`` 判
「对象标识重复」，从入库到发布之间的窗口无人把关。

存量可能已有重名（生成端只在单个证据块内去碰撞，分块生成的同名表会漏网）。故加
约束前先就地消歧：组内保留第一个，其余改用源表名 snake，仍撞则追加数字后缀——与
``app.services.object_naming.dedupe_object_names`` 同一套规则，这里用纯 SQL 重写一遍，
避免迁移依赖应用层代码（模型日后变了迁移仍要能跑）。

**本文件是补回丢失的迁移**：修订号 ``a1b2c3d4e5f7`` 曾被应用到开发库（其
``object_types`` 已带 ``uq_object_type_ontology_name``、``alembic_version`` 也停在此号），
但文件从未进版本库，git 历史里查无此物——于是开发库停在一个树上不存在的修订上，
``alembic upgrade`` 会以「Can't locate revision」失败。沿用原修订号把它补回来，既让存量
开发库重新对得上，也让新库能建出同样的约束。故 ``upgrade`` 做成幂等：约束已在就跳过。

Revision ID: a1b2c3d4e5f7
Revises: f1a2b3c4d5e6
Create Date: 2026-08-11
"""

from __future__ import annotations

import re
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a1b2c3d4e5f7"
down_revision: Union[str, Sequence[str], None] = "f1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CONSTRAINT = "uq_object_type_ontology_name"

_NON_ALNUM = re.compile(r"[^a-zA-Z0-9]+")
# urn:li:dataset:(urn:li:dataPlatform:<platform>,<schema>.<table>,<env>)
_URN_TABLE = re.compile(r"\(.*?,(?P<path>.+),[^,()]+\)\s*$")


def _table_name_from_ref(source_ref: str | None) -> str:
    """源表名 snake（与 object_naming.table_name_from_ref 同规则）。"""
    if not source_ref:
        return ""
    match = _URN_TABLE.search(source_ref)
    path = match.group("path") if match else source_ref
    table = path.rsplit(".", 1)[-1].strip()
    if table.startswith("tab") and len(table) > 3 and table[3].isupper():
        table = table[3:]
    return _NON_ALNUM.sub("_", table).strip("_").lower()


def _dedupe_existing(conn: sa.engine.Connection) -> None:
    """把存量重名就地消歧。无重名时不写任何一行。"""
    dupes = conn.execute(
        sa.text(
            "SELECT ontology_id, name FROM object_types "
            "GROUP BY ontology_id, name HAVING COUNT(*) > 1"
        )
    ).fetchall()
    if not dupes:
        return

    for ontology_id, name in dupes:
        # 该本体内已占用的全部名字，避免消歧后撞上无辜的单例名。
        used = {
            row[0]
            for row in conn.execute(
                sa.text("SELECT name FROM object_types WHERE ontology_id = :oid"),
                {"oid": ontology_id},
            )
        }
        members = conn.execute(
            sa.text(
                "SELECT id, source_ref FROM object_types "
                "WHERE ontology_id = :oid AND name = :name ORDER BY created_at, id"
            ),
            {"oid": ontology_id, "name": name},
        ).fetchall()
        # 组内保留第一个，其余改名。
        for obj_id, source_ref in members[1:]:
            base = _table_name_from_ref(source_ref) or name
            final, suffix = base, 2
            while final in used:
                final = f"{base}_{suffix}"
                suffix += 1
            used.add(final)
            conn.execute(
                sa.text("UPDATE object_types SET name = :new WHERE id = :id"),
                {"new": final, "id": obj_id},
            )


def _has_constraint(bind: sa.engine.Connection) -> bool:
    inspector = sa.inspect(bind)
    return any(
        uc.get("name") == _CONSTRAINT
        for uc in inspector.get_unique_constraints("object_types")
    )


def upgrade() -> None:
    bind = op.get_bind()
    if _has_constraint(bind):
        return  # 开发库已由丢失的那次迁移建好，勿重复创建
    _dedupe_existing(bind)
    with op.batch_alter_table("object_types", schema=None) as batch_op:
        batch_op.create_unique_constraint(_CONSTRAINT, ["ontology_id", "name"])


def downgrade() -> None:
    if not _has_constraint(op.get_bind()):
        return
    with op.batch_alter_table("object_types", schema=None) as batch_op:
        batch_op.drop_constraint(_CONSTRAINT, type_="unique")
