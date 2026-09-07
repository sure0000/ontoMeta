"""智能关系补充的异步任务。

**为什么必须异步**：P2 真机实测——bundle 命中缓存后，LLM 判定这一步在自建
glm-5.2-fp8 上要 ~430s。画布上点一下按钮不可能同步等这么久（前端 fetch、
反向代理都会先断），所以推断必须落成任务 + 轮询进度。

**为什么不进子进程**：草稿生成走了分离子进程（``app/jobs/draft_worker``），因为它是
核心流程且更长。关系推断是 IO 密集的单次长调用，在 API 进程里 ``asyncio`` 挂着不阻塞
事件循环；代价是开发热重载会把跑到一半的任务打断——由启动时的陈旧任务回收兜底，
用户重新点一次即可。真要做到 reload 免疫再抬进子进程。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

#: 还没跑完的状态——同一个域同时只允许一个在跑。
ACTIVE_INFERENCE_STATUSES = ("queued", "running")


def _uuid() -> str:
    return str(uuid.uuid4())


class RelationInferenceTask(Base):
    __tablename__ = "relation_inference_tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    domain_context_id: Mapped[str] = mapped_column(
        ForeignKey("domain_contexts.id"), index=True
    )
    #: 成功后指向本轮候选的 run_id，失败时为空。
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)

    #: queued / running / succeeded / failed
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: 成功后的统计（族数 / 各判定条数），给前端直接显示，免得再查一遍。
    summary_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
