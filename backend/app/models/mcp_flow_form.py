"""一次性网页表单：交互式建数流程的兜底渲染面。

客户端有原生问答工具（dsh 的 ``ask_user_question``、Claude Code 的 ``AskUserQuestion``）
时，一环的表单直接在对话里渲染，不需要这张表。没有那类工具的通用 Agent 只能摆文本清单——
这时把这一环变成 ontoMeta 控制台上的一个真表单链接，用户点开填、提交，Agent 再取回填值继续。

存的是**这一环的入参**（kind / ontology / 已有答案），不存字段与候选：候选随本体和数据源
实时变，存一份快照只会让人在一张过期的表单上做决定。
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class McpFlowForm(Base):
    __tablename__ = "mcp_flow_forms"
    # 长轮询按 (status, created_at) 找待提交的表单，清理过期行也走同一条。
    __table_args__ = (Index("ix_mcp_flow_forms_status", "status", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    kind: Mapped[str] = mapped_column(String(32))
    ontology_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    #: 这张表单处在哪一步：``decide``（还定不下来的参数）/ ``review``（执行审查）。
    stage: Mapped[str] = mapped_column(String(32))
    goal: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    #: 发单时的累计答案（JSON）。提交后合并用户填的值写回同一列的 submitted_json。
    answers_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="{}", server_default="{}"
    )
    submitted_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", server_default="pending")
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
