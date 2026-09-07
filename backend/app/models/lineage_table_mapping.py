"""SQL 表名 → DataHub URN 的人工映射。

**为什么需要**：``_classify`` 把对不上 URN 的边判 ``blocked``，理由是「落点表未在
DataHub 找到」。此前的唯一出路是重扫——但重扫用的是同一套 ``inventory.resolve``，
结果一模一样。真实原因往往只是库名前缀不同、大小写不同、或者代码里用的是视图别名，
人一眼就看得出该对到哪张表，系统却没有地方让他说。

映射是**域级**的：同一个客户的多个代码包里，``dl_ods.v_wifi_info`` 指的都是同一张表，
说一次就够，重扫时自动套用。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class LineageTableMapping(Base):
    __tablename__ = "lineage_table_mappings"
    __table_args__ = (
        # 同一个域里一个 SQL 表名只能指向一个 URN——两条冲突的映射会让重扫结果随机。
        UniqueConstraint(
            "domain_context_id", "sql_table", name="uq_lineage_mapping_domain_table"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    domain_context_id: Mapped[str] = mapped_column(
        ForeignKey("domain_contexts.id"), index=True
    )
    #: 代码包里写的表名，**按小写存**——SQL 里大小写不一致是常态。
    sql_table: Mapped[str] = mapped_column(String(512), index=True)
    #: 映射到的 DataHub URN，以及它在 DataHub 里的表名（回显用，免得再查一次）。
    target_urn: Mapped[str] = mapped_column(String(1024))
    target_table: Mapped[str] = mapped_column(String(512))

    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
