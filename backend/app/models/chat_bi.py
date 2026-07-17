from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, func
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
