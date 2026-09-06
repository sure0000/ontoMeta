"""删掉决策账本与任务链三张表。

**决策账本**（``chat_bi_decision_records``）按 ``conversation_id`` 组织，随 Data Agent
的六环会话一起退场。它记的三件事——机器提了什么、人改了什么、谁拍的板——改由治理制品
自己承载：``machine_baseline`` / ``overridden_fields`` / ``created_by`` / ``confirmed_by``
/ ``agent_execution_approved_by``。制品不绑会话，通用 agent 经 MCP 建的任务同样记得下。

**任务链**（``governance_task_pipelines`` 及其步骤表）只做两件事：记住下一步、把上游
落点接给下游。通用 agent 在会话里天然做这两件，链在服务端再做一遍就是第二套编排。
多任务间的依赖仍由 ``lineage_scheduler`` 从血缘推导。

**数据不迁移**：账本是观察层，从不授权任何执行；链的每一步本来就是一条独立制品，制品
连同它们的回执原样留在 ``governance_artifacts``。丢的是"第几环、哪条链"这两个视角。

Revision ID: drop_ledger_pipelines_20260905
Revises: artifact_provenance_20260905
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "drop_ledger_pipelines_20260905"
down_revision: str | Sequence[str] | None = "artifact_provenance_20260905"
branch_labels = None
depends_on = None

# 顺序有讲究：步骤表有指向链表的外键，必须先删子表。
_TABLES = (
    "chat_bi_decision_records",
    "governance_task_pipeline_steps",
    "governance_task_pipelines",
)


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    for table in _TABLES:
        if table in existing:
            op.drop_table(table)


def downgrade() -> None:
    """不重建。

    这三张表的建表 DDL 散在 5a881e5c0024 / 7ff98a08a656 / c2d3e4f5a6b7 / c1385f0ad1e8 /
    d467f452d8b8 五个修订里，在这里抄一份回来只会得到第二份定义；真要回退请降到本修订
    之前。**空实现是刻意的，不是遗漏。**
    """
