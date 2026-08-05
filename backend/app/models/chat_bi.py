from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

import uuid

from app.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class ChatBiConversation(Base):
    __tablename__ = "chat_bi_conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    domain_id: Mapped[str] = mapped_column(
        ForeignKey("domain_contexts.id"), index=True
    )
    title: Mapped[str] = mapped_column(String(255), default="新对话")
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


class ChatBiExternalTool(Base):
    """配置驱动的外部工具（P4：免改代码扩展 Data Agent 能力）。

    运维在此注册一个外部 HTTP 工具（名称 + 描述 + JSON-Schema 入参 + 端点 + 可选鉴权头），
    **启用**即等于把它交给 Data Agent——启用的工具会被投影成 OpenAI 函数 schema 注入 agent
    工具集（curated：按域过滤 + 数量封顶，避免全量目录撑爆 prompt），模型据描述自主调用，
    结果经通用 HTTP executor 取回并封顶字符数。

    作用域：``domain_id`` 为空=全局可用，否则仅该域。``name`` 全局唯一且不得与原生工具同名。
    ``auth_header`` 是机密：写入后接口不回显（只给 has_auth），遵循设置页机密回显约定。
    """

    __tablename__ = "chat_bi_external_tools"
    __table_args__ = (
        UniqueConstraint("name", name="uq_chat_bi_external_tools_name"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(64), index=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True, default=None)
    description: Mapped[str] = mapped_column(Text)
    # OpenAI function `parameters`（JSON-Schema 对象）序列化文本
    parameters_json: Mapped[str] = mapped_column(Text, default="{}")
    method: Mapped[str] = mapped_column(String(10), default="POST")
    url: Mapped[str] = mapped_column(Text)
    # 机密：整串作为一个请求头值（如 "Bearer xxx"），接口不回显
    auth_header: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # 为空=全局；否则仅该数据域可见
    domain_id: Mapped[str | None] = mapped_column(String(36), nullable=True, default=None, index=True)
    result_max_chars: Mapped[int] = mapped_column(Integer, default=4000)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
