"""ontologies: unique (domain_context_id) —— 一域一本体

见 ``docs/ONTOLOGY_LIFECYCLE_REDESIGN.md``。``publish()`` 历史上把草稿行**就地**翻成
published，域内于是不再有 draft；再点生成时「找 draft 行」必然落空 → 新建一个空白
本体行，三方合并基线全空、人工修订跨发布边界失忆；再发布一次，域内就攒出两个
published 本体，本体浏览页与 Agent 可检索集一起翻倍，版本历史也整段丢失。

写侧已全部改走 ``ontology_workspace.get_or_create_working_ontology``（按域取行、不看
status），这里把不变量钉进库：**一个数据域至多一行本体**。

存量收敛规则（与 ``get_working_ontology`` 同一套，此处用纯 SQL 重写，避免迁移依赖
应用层代码）：
- 已发布行优先——人工权威与对外服务都在它身上；多个已发布时取 version 最大者，
  同版本取 **created_at 最早** 的那行（分叉总是更新的，原始血脉在旧行）。
- 没有已发布行时取 created_at 最新的草稿行。

多余行连同其对象/属性/关系/证据一并删除。但**绝不静默销毁用户资产**：若多余行上
挂着数据应用、物化契约、治理制品或任务流水线，迁移直接报错并列出 id，交人工处置。

Revision ID: 4aa435f23621
Revises: 7ff98a08a656
Create Date: 2026-08-21
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "4aa435f23621"
down_revision: Union[str, Sequence[str], None] = "7ff98a08a656"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEX = "uq_ontology_domain_context"

# 多余本体行上一旦挂着这些资产，就不是「机器重复生成的副本」了，不能自动删。
_ASSET_TABLES = (
    "data_apps",
    "data_app_widgets",
    "materialization_contracts",
    "governance_artifacts",
    "governance_task_pipelines",
)


def _table_exists(bind, name: str) -> bool:
    return name in sa.inspect(bind).get_table_names()


def _pick_keeper(rows: list[tuple]) -> str:
    """rows: [(id, status, version, created_at, published_at), ...] → 保留哪一行。"""
    published = [r for r in rows if r[1] == "published"]
    if published:
        # version 越大越靠后；同 version 取 created_at 最早（原始血脉）。
        return sorted(published, key=lambda r: (-(r[2] or 0), r[3] or ""))[0][0]
    return sorted(rows, key=lambda r: (r[3] or ""), reverse=True)[0][0]


def _drop_ontologies(bind, ids: list[str]) -> None:
    if not ids:
        return
    marks = ",".join(f":i{n}" for n in range(len(ids)))
    params = {f"i{n}": v for n, v in enumerate(ids)}

    obj_ids = [
        r[0]
        for r in bind.execute(
            sa.text(f"SELECT id FROM object_types WHERE ontology_id IN ({marks})"),
            params,
        )
    ]
    logic_ids = [
        r[0]
        for r in bind.execute(
            sa.text(f"SELECT id FROM business_logics WHERE ontology_id IN ({marks})"),
            params,
        )
    ]
    prop_ids: list[str] = []
    if obj_ids:
        om = ",".join(f":o{n}" for n in range(len(obj_ids)))
        op_params = {f"o{n}": v for n, v in enumerate(obj_ids)}
        prop_ids = [
            r[0]
            for r in bind.execute(
                sa.text(f"SELECT id FROM properties WHERE object_type_id IN ({om})"),
                op_params,
            )
        ]

    def _delete_in(table: str, column: str, values: list[str]) -> None:
        if not values or not _table_exists(bind, table):
            return
        m = ",".join(f":v{n}" for n in range(len(values)))
        bind.execute(
            sa.text(f"DELETE FROM {table} WHERE {column} IN ({m})"),
            {f"v{n}": v for n, v in enumerate(values)},
        )

    _delete_in("business_logic_property_bindings", "property_id", prop_ids)
    _delete_in("business_logic_property_bindings", "business_logic_id", logic_ids)
    _delete_in("business_logic_object_bindings", "object_type_id", obj_ids)
    _delete_in("business_logic_object_bindings", "business_logic_id", logic_ids)
    _delete_in("business_logics", "ontology_id", ids)
    _delete_in("properties", "object_type_id", obj_ids)
    _delete_in("relation_types", "ontology_id", ids)
    _delete_in("object_types", "ontology_id", ids)
    _delete_in("draft_evidences", "ontology_id", ids)
    _delete_in("change_confirmations", "ontology_id", ids)
    _delete_in("semantic_index_entries", "ontology_id", ids)

    if _table_exists(bind, "draft_generation_tasks"):
        bind.execute(
            sa.text(
                "UPDATE draft_generation_tasks SET ontology_id = NULL "
                f"WHERE ontology_id IN ({marks})"
            ),
            params,
        )
    _delete_in("ontologies", "id", ids)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _INDEX in {i["name"] for i in inspector.get_indexes("ontologies")}:
        return

    rows = list(
        bind.execute(
            sa.text(
                "SELECT id, status, version, created_at, published_at, "
                "domain_context_id FROM ontologies"
            )
        )
    )
    by_domain: dict[str, list[tuple]] = {}
    for r in rows:
        by_domain.setdefault(r[5], []).append(tuple(r[:5]))

    doomed: list[str] = []
    for domain_id, group in by_domain.items():
        if len(group) < 2:
            continue
        keeper = _pick_keeper(group)
        doomed.extend(r[0] for r in group if r[0] != keeper)

    if doomed:
        blocked: list[str] = []
        marks = ",".join(f":i{n}" for n in range(len(doomed)))
        params = {f"i{n}": v for n, v in enumerate(doomed)}
        for table in _ASSET_TABLES:
            if not _table_exists(bind, table):
                continue
            hits = [
                r[0]
                for r in bind.execute(
                    sa.text(
                        f"SELECT DISTINCT ontology_id FROM {table} "
                        f"WHERE ontology_id IN ({marks})"
                    ),
                    params,
                )
            ]
            blocked.extend(f"{table}:{h}" for h in hits)
        if blocked:
            raise RuntimeError(
                "存在多余的本体行，且其上挂有下游资产，迁移拒绝自动删除："
                + "、".join(blocked)
                + "。请先在这些资产上改绑到该域保留的本体行，或手工清理后重跑迁移。"
            )
        _drop_ontologies(bind, doomed)

    op.create_index(_INDEX, "ontologies", ["domain_context_id"], unique=True)


def downgrade() -> None:
    op.drop_index(_INDEX, table_name="ontologies")
