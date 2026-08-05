"""治理规约的落库记录（G3）。

**为什么只存「激活哪版 + 审计」而非可执行 JSON**：规约条款的**判定逻辑**住在
``governance/lint.py`` 与 ``agents/validation.py``——一条纯数据、linter 不知道怎么执行的
规则是死规则。故「规约」由代码定义版本常量（见 ``services/governance_standard._REGISTRY``），
DB 记录的是「当前发布哪个版本」以及历次发布的审计轨迹；``payload_json`` 只是那一刻规约的
只读快照，供人查看/diff，不参与运行时判定（运行时按 version 回注册表取代码常量）。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class GovernanceStandardRecord(Base):
    __tablename__ = "governance_standard_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    version: Mapped[str] = mapped_column(String(50))
    # draft（拟发布）| published（当前生效）| superseded（被后来的发布顶替）。
    status: Mapped[str] = mapped_column(String(20), default="published")
    # 发布时规约的只读快照（to_dict）。仅供审计/diff，运行时不读它。
    payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    note: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    activated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
