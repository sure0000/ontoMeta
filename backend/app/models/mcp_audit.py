"""MCP 工具调用审计（append-only）。

**为什么单独建表、不复用 ``ChatBiDecisionRecord``**：那张表是 Data Agent 六环人工决策
留痕，硬绑 ``conversation_id``（与会话同生命周期），装不下「一次 MCP 工具调用」——MCP
没有会话、没有六环，记录的是「谁、用什么身份、调了哪个工具、成没成、被不被授权拦下」。

**审计不可篡改**：只追加（无 ``updated_at``），记录一旦落地不再改写。这是它作为审计源的
前提——能被回改的日志不是审计。

**只观察、不授权**：写审计失败绝不能影响工具调用本身（见 ``app.mcp.audit.record_call``
的吞异常），审计里有什么也不改变任何一次调用的放行结果。授权判定的唯一权威是各工具的
``required_role`` + 服务器的集中强制。
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class McpAuditLog(Base):
    __tablename__ = "mcp_audit_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), index=True
    )

    # 谁：身份来自启动 MCP 服务器的客户端在 env 里传入的 Token（stdio 一会话一身份）。
    # 未提供 Token 的匿名本地会话 principal_id 为空，role 取 mcp_default_role。
    principal_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    principal_role: Mapped[str | None] = mapped_column(String(30), nullable=True)
    client_type: Mapped[str] = mapped_column(String(30), default="mcp_local")

    # 什么：工具名 + 脱敏后的入参（凭据类键已 redact，超限截断）。
    tool_name: Mapped[str] = mapped_column(String(100), index=True)
    arguments_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 结果：成功与否、是否被授权拦下（denied=True 即 403 类事件，与业务失败区分）、错误摘要。
    success: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    denied: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", index=True
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
