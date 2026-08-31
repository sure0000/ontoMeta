from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

import json
import uuid

from app.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


# 会话未命名时的占位标题。判定「这个会话还没被命名过」只认这一个值，
# 服务层的标题自愈据此决定要不要用首问覆写。
DEFAULT_CONVERSATION_TITLE = "新对话"


class ChatBiConversation(Base):
    __tablename__ = "chat_bi_conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # 多域会话：domain_ids 为会话作用的数据域集合（JSON 数组字符串）。
    #   非空 = 跨域会话（如 ["A","B"]）；空/NULL = 不选域（全域通盘）。
    # 保留旧 domain_id 列做兼容与锚点（旧数据迁移后 domain_ids=[domain_id]）。
    domain_ids_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    domain_id: Mapped[str | None] = mapped_column(
        ForeignKey("domain_contexts.id"), index=True, nullable=True
    )
    title: Mapped[str] = mapped_column(String(255), default=DEFAULT_CONVERSATION_TITLE)
    category: Mapped[str | None] = mapped_column(String(100), nullable=True, default=None)
    is_pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    messages: Mapped[list["ChatBiMessage"]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="ChatBiMessage.created_at",
    )

    # ---- 多域作用域读写 ----
    # domain_ids 的语义：None/空 = 不选域（全域通盘）；非空 = 显式选定的域集合。
    # 旧会话只有 domain_id：effective_domain_ids 兜底为 [domain_id]。
    @property
    def domain_ids(self) -> list[str]:
        if self.domain_ids_json:
            try:
                ids = json.loads(self.domain_ids_json)
                if isinstance(ids, list):
                    return [str(x) for x in ids if x]
            except (TypeError, ValueError):
                pass
        return [self.domain_id] if self.domain_id else []

    def set_domain_ids(self, ids: list[str] | None) -> None:
        """设置作用域。None 或空列表 = 不选域（全域通盘），存空数组以区别于旧 NULL。"""
        cleaned = [str(x) for x in (ids or []) if x]
        self.domain_ids_json = json.dumps(cleaned)
        # 同步锚点 domain_id：取首个，便于旧查询/记忆锚定；空则置空。
        self.domain_id = cleaned[0] if cleaned else None


class ChatBiMessage(Base):
    __tablename__ = "chat_bi_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("chat_bi_conversations.id"), index=True
    )
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    conversation: Mapped["ChatBiConversation"] = relationship(
        back_populates="messages"
    )


class ChatBiConversationTask(Base):
    """会话 ↔ 数据任务（治理制品）的关联（P1：跨轮任务记忆）。

    用户在某会话里对 Data Agent 的任务提案点「去校验并执行」建出一条 GovernanceArtifact 后，
    前端把 (会话, 制品) 关联落这张表；后续该会话问「那个任务好了吗」时，get_task_status 无需
    用户重报 id，即可解析出本会话产出的任务并回读实时状态。

    对 artifact_id 用软引用（不设 FK）：治理制品流水线与 chat 解耦，制品的权威在 agent 侧，
    这里只记「哪个会话催生了哪个任务」，状态实时经 agent_pipeline.get 回读。
    """

    __tablename__ = "chat_bi_conversation_tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("chat_bi_conversations.id"), index=True
    )
    artifact_id: Mapped[str] = mapped_column(String(36), index=True)
    kind: Mapped[str | None] = mapped_column(String(40), nullable=True, default=None)
    intent: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    # 催生这条任务的那张表单向导的 confirmation_id。**闭环按任务分开的唯一接缝**：
    # 前三环（需求/本体/数据）在制品还不存在时就确认了，只能按 confirmation_id 归属；
    # 后三环（方案/执行/结果）按 artifact_id 归属。不落这一列，一条会话里建的多个任务
    # 的前三环就只能混成一坨，谁也说不清哪一环是给哪条任务确认的。
    # 历史行为空：那些任务的前三环无从归属，闭环里如实标灰，不猜。
    confirmation_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, default=None, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ChatBiDomainMemory(Base):
    """按数据域沉淀的「高频使用」记忆（P3：跨会话记忆 / 个性化）。

    每次 Data Agent 给出**已接地**的回答后，把这次命中的对象/口径按 (域, 实体) 累加计数——
    形成「本域实际上常被问什么」的动态画像。它与静态的域语义卡（按结构重要性）互补：
    结构重要 ≠ 真实使用。召回时取 top-N 作为**软提示**注入系统提示，让复现问题少绕检索、
    少重复澄清、更快取数。

    作用域=数据域（本系统按角色/管理鉴权，无逐用户身份）；将来若有用户身份，加 user_id 列即可。
    ``ref_id`` 用软引用（不设 FK）：实体权威在本体侧，这里只累计使用度，召回仅作提示、以检索为准。
    """

    __tablename__ = "chat_bi_domain_memory"
    __table_args__ = (
        UniqueConstraint("domain_id", "ref_kind", "ref_id", name="uq_chat_bi_domain_memory_ref"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    domain_id: Mapped[str] = mapped_column(
        ForeignKey("domain_contexts.id"), index=True
    )
    # object_type | business_logic
    ref_kind: Mapped[str] = mapped_column(String(30))
    ref_id: Mapped[str] = mapped_column(String(36))
    label: Mapped[str | None] = mapped_column(String(255), nullable=True, default=None)
    hit_count: Mapped[int] = mapped_column(Integer, default=0)
    last_used_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

