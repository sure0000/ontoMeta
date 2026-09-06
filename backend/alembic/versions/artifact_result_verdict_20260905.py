"""制品记「人认为这次结果对不对」。

**跑完了 ≠ 跑对了**：``status`` 与 ``execution_receipt_json`` 说的是系统这侧发生了什么
（DAG 提交成功、Airflow 终态、写了多少行），答不了「搬过来的数对不对」。回执自陈成功而
数据其实不对，本仓真实发生过（见 receipt-failure-vs-artifact-status）。

这份判断此前记在按会话组织的决策账本的「结果」环里，随账本一起没了。现在落到制品自己身上：
人在任务详情里点，或通用 agent 按 skill 问出用户答复后经 ``confirm_task_result`` 回写；
``result_via`` 区分这两条来路（词汇与 ``created_via`` 同一套）。

**不给默认值、不从 status 推**：没人表态就一直是 NULL。存量任务同理——回填一个"想必是成功了"
比空着更坏，那会让人以为有人看过。

Revision ID: artifact_result_verdict_20260905
Revises: drop_ledger_pipelines_20260905
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "artifact_result_verdict_20260905"
down_revision: str | Sequence[str] | None = "drop_ledger_pipelines_20260905"
branch_labels = None
depends_on = None

_COLUMNS = (
    ("result_outcome", sa.String(length=20)),
    ("result_note", sa.Text()),
    ("result_confirmed_by", sa.String(length=255)),
    ("result_confirmed_at", sa.DateTime()),
    ("result_via", sa.String(length=30)),
)


def _existing() -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns("governance_artifacts")}


def upgrade() -> None:
    # 逐列判断：本仓测试与本地起服务都走 create_all，存量库可能已有这些列，
    # 重复 ADD COLUMN 在 SQLite 上是硬错。
    existing = _existing()
    for name, type_ in _COLUMNS:
        if name not in existing:
            op.add_column("governance_artifacts", sa.Column(name, type_, nullable=True))
    if "result_outcome" not in existing:
        op.create_index(
            "ix_governance_artifacts_result_outcome",
            "governance_artifacts",
            ["result_outcome"],
        )


def downgrade() -> None:
    existing = _existing()
    if "result_outcome" in existing:
        op.drop_index(
            "ix_governance_artifacts_result_outcome", table_name="governance_artifacts"
        )
    for name, _type in _COLUMNS:
        if name in existing:
            op.drop_column("governance_artifacts", name)
