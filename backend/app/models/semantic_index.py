"""语义检索索引（P1.5）：已发布实体的嵌入向量。

**为什么不用 pgvector**（与 DATA_AGENT_V2_PLAN 原设计的偏离，理由见 §8.9）：
它要装 Postgres 扩展（部署前置条件），而测试跑 SQLite——引入一条 CI 覆盖不到的路径，
与 P0 立下的「每期都要可回归」正好相反。本项目一个域的可检索实体是**百到千级**，
暴力余弦足够快，普通表即可，两种数据库都跑得动。规模真上来再换 pgvector 不迟。

向量以 JSON 文本存储：可移植、可读、便于排查；读取后在进程内缓存成 float 列表。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class SemanticIndexEntry(Base):
    """一个已发布实体的可检索文本 + 其嵌入向量。"""

    __tablename__ = "semantic_index_entries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    ontology_id: Mapped[str] = mapped_column(ForeignKey("ontologies.id"), index=True)
    # 本体版本：发布会 +1，据此判断索引是否过期（无需另设失效标记）
    ontology_version: Mapped[int] = mapped_column(Integer, default=0, index=True)
    # object_type / business_logic
    kind: Mapped[str] = mapped_column(String(50), index=True)
    entity_id: Mapped[str] = mapped_column(String(36), index=True)
    # 参与嵌入的文本（名称 + 显示名 + 描述/口径摘要），留存以便排查召回问题
    text: Mapped[str] = mapped_column(Text)
    # JSON 数组文本。维度由 embedding 服务决定，可按 agent_embedding_dim 截断
    vector_json: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(String(100), default="")
    dim: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


__all__ = ["SemanticIndexEntry"]
