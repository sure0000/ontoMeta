"""智能关系补充的候选：一个键族一行。

**为什么按族存，不按对存**：一个族展开成两两关系是 O(n²)——jwsp 实测 12 个族展开是
15244 对。候选表要给人审，人审的单位是「这 40 张表里的 bl_bh/ry_bh/zbr_bh 是不是同一个
人员编号」，不是 15244 条「A 和 B 有关系吗」。两两关系在 apply 时按需展开，
展开前必须有人对整族表过态。

**为什么不塞进 LineagePackageEdge**：那张表的语义是「一条血缘边」（字段是 source_file /
join_key / applied_at）。硬塞会让「边」这个词同时指血缘和关联——而这两件事的语义分叉
正是本方案要守住的（血缘是 derivation，关联是 foreign_key，见 docs 第 3 节）。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class RelationCandidateFamily(Base):
    """一个键族 + 它的 LLM 判定 + 人工表态。"""

    __tablename__ = "relation_candidate_families"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    domain_context_id: Mapped[str] = mapped_column(
        ForeignKey("domain_contexts.id"), index=True
    )
    #: 一次推断批次。同一个域可以反复推断，靠它区分「这是哪一轮的产物」。
    run_id: Mapped[str] = mapped_column(String(36), index=True)

    #: 机械层给的族标识（值形状的哈希）与值形状本身——重新推断时靠 family_id 对齐人工表态。
    family_id: Mapped[str] = mapped_column(String(64), index=True)
    value_shape: Mapped[str] = mapped_column(String(64))
    #: 代表值 / 成员列（JSON 文本）。成员带 distinct 与 rows，基数据此算出，不问模型。
    sample_values_json: Mapped[str] = mapped_column(Text, default="[]")
    members_json: Mapped[str] = mapped_column(Text, default="[]")
    table_count: Mapped[int] = mapped_column(Integer, default=0)
    column_count: Mapped[int] = mapped_column(Integer, default=0)

    #: LLM 判定：entity_key / dimension_code / not_a_key
    verdict: Mapped[str] = mapped_column(String(20), index=True)
    entity_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    key_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: 两两关系命名用的谓词（「涉及」「归属」…）。
    predicate: Mapped[str | None] = mapped_column(String(64), nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    #: 判据原文——人在画布上审的就是这句话，必须原样留着。
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: proposed（待人工表态）/ confirmed / rejected / applied
    state: Mapped[str] = mapped_column(String(20), default="proposed", index=True)
    decided_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
