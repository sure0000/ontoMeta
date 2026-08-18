"""Data Agent 人工决策留痕（Decision Record）。

一次对话里人在关键节点拍的板：需求确认 → 本体确认 → 数据确认 → 执行方案确认 →
执行任务 → 结果确认。每条记录回答四个问题——**谁、在哪一环、机器提了什么、人最终定了什么**。

**为什么必须新建表，而不是靠既有字段拼**（这条是本模块的立论）：

1. ``GovernanceArtifact`` 上的 ``confirmed_by`` / ``confirmed_at`` 会被抹掉。
   ``agent_pipeline.edit()`` 在用户改动 spec 时显式执行 ``confirmed_by = None``——
   语义上是对的（旧确认对新 spec 无效），但**代价是"人确认过一次又改了参数"这个事实
   被物理删除**。事后再问"这个任务当初是谁拍的板、改过几轮"，库里已无从答起。
2. ``ChangeConfirmation`` 硬绑 ``ForeignKey("ontologies.id")``，装不下同步/物化/看板/
   数据源这些非本体决策。
3. ``agent_trace`` 是默认关闭的 JSONL，模块头明写"零 schema 债"——它是 eval 回放用的
   观测增强，不是审计源。

故本表是**追加式（append-only）**的：记录一旦落地不再改写（无 ``updated_at``），
这正是它不可被上述任何一处替代的原因。

**只记录，不授权**：本表是观察层。执行门槛的唯一权威仍是 ``GovernanceArtifact.status``，
"未确认不得执行"由 ``agent_pipeline`` 把守。账本里有什么记录都不能让一条未确认的制品被执行——
``test_agent_pipeline`` 里有用例把这条钉死。

命名避让：``ledger`` 一词在本仓已被 ``agent_grounding.FactLedger`` 占用（每轮的接地断言
账本，不落库），故本表用 ``decision``。
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class DecisionNode(str, enum.Enum):
    """人工确认闭环的六环。

    取值是**稳定契约**：前端闭环视图按它分组、分析查询按它聚合，改名要同步前端。
    """

    REQUIREMENT = "requirement"  # 需求确认：澄清选项、表单填写
    ONTOLOGY = "ontology"  # 本体确认：口径/对象定义的创建与改写
    DATA = "data"  # 数据确认：数据源接入、口径认可、本域约定
    PLAN = "plan"  # 执行方案确认：任务参数定稿、制品 confirm
    EXECUTE = "execute"  # 执行任务：制品 execute
    RESULT = "result"  # 结果确认：结果验收、落成数据应用
    OTHER = "other"  # 归一兜底：未知取值不丢记录（见 record_decision）


class DecisionOutcome(str, enum.Enum):
    """人在该环的最终取态。"""

    ACCEPTED = "accepted"  # 原样接受机器提案
    MODIFIED = "modified"  # 接受但改过参数（overridden_fields 非空）
    REJECTED = "rejected"  # 明确否掉
    SKIPPED = "skipped"  # 明确跳过/稍后再说


# 六环的固定展示顺序与中文名。闭环视图恒展示全部六环（未到达的标灰而非隐藏——
# "哪一环没走"正是管理要看的东西），故顺序与文案在此定义一份，前后端共用。
NODE_SEQUENCE: tuple[tuple[str, str], ...] = (
    (DecisionNode.REQUIREMENT.value, "需求确认"),
    (DecisionNode.ONTOLOGY.value, "本体确认"),
    (DecisionNode.DATA.value, "数据确认"),
    (DecisionNode.PLAN.value, "执行方案确认"),
    (DecisionNode.EXECUTE.value, "执行任务"),
    (DecisionNode.RESULT.value, "结果确认"),
)


class ChatBiDecisionRecord(Base):
    """一条人工决策留痕。

    锚定方式：``conversation_id`` 必填（FK，与会话同生命周期）；``message_id`` /
    ``block_id`` 尽力而为——流式消息在落库前前端拿不到 id，且 ``answer_to_blocks``
    产出的 ``b0/b1…`` 是**位置序号、每次投影重排**（见 chat_bi_blocks._add），
    故两者都可空，仅作辅助定位，不做外键也不做唯一性依据。
    """

    __tablename__ = "chat_bi_decision_records"
    __table_args__ = (
        # 可空唯一：SQLite/MySQL 下 NULL 互不相等，故无自然键的记录永不被误合并，
        # 有自然键的（同一表单重复提交、同一制品重复确认）天然幂等。
        UniqueConstraint("dedup_key", name="uq_chat_bi_decision_dedup"),
        Index("ix_chat_bi_decision_conv_seq", "conversation_id", "seq"),
        Index("ix_chat_bi_decision_node_created", "node", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)

    # ---- 锚定 ----
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("chat_bi_conversations.id"), index=True
    )
    message_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    block_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # 会话内序号，仅用于展示排序的稳定性；并发下重号可接受（created_at 兜底）。
    seq: Mapped[int] = mapped_column(Integer, default=0)

    # ---- 分类 ----
    node: Mapped[str] = mapped_column(String(30), index=True)
    # 同一环下的细分场景（form / clarify / artifact_confirm / datasource…），
    # 便于分析时下钻，但不进枚举——新增交互点不该要求改模型。
    stage: Mapped[str | None] = mapped_column(String(40), nullable=True)
    trigger: Mapped[str | None] = mapped_column(String(40), nullable=True)
    outcome: Mapped[str] = mapped_column(String(20), index=True)

    # ---- 责任人 ----
    # **服务端从 request.state 取，前端传值一律忽略**——否则"谁确认的"可被客户端伪造，
    # 追踪与管理就失去依据。用共享 admin token 时 principal_id 为 None（auth.resolve_principal
    # 对 admin token 不查库），此时只有 subject_role 可考，这是运营前提不是代码问题。
    subject_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    subject_role: Mapped[str | None] = mapped_column(String(30), nullable=True)

    # ---- 内容 ----
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 显式落两份而不是只落 diff：diff 的键名脱离当时上下文读不出意义，
    # 而"事后追溯"恰恰要求多年以后不看原对话也能读懂人当初定了什么。
    proposed_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    chosen_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 与 GovernanceArtifact.overridden_fields **同名同格式**（JSON 字符串数组）：
    # 复用本仓既有的"人相对机器基线改过哪些键"的表达，而不是新发明一套。
    # 由服务端从两份 JSON 派生，不由调用方传——避免每个调用点各写一份 diff 逻辑。
    overridden_fields: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ---- 产物软引用（不设 FK：产物的权威在各自模块，这里只记"这次决策产出了什么"）----
    # artifact | business_logic | data_app | datasource | domain | pipeline | preference
    ref_kind: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    ref_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)

    dedup_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), index=True
    )

    # 无 updated_at：追加式账本，记录不改写。改判要追加新记录而不是覆盖旧的——
    # "人改过主意"本身就是有价值的信息。
