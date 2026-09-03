"""血缘补录：代码包与它扫出来的边。

**为什么要落库**：补录是长期活，同一个域会陆续收到好几个包，扫完不一定当场上报。
包必须留档——什么时候投的、扫出多少边、上报了没有、当时几张表脱离孤岛。没有这份
历史，「这个包是不是已经补过了」就只能靠人记。

边也一起留档：上报是幂等的（DataHub 对已存在的边不重复建），但「哪些边是这个包给的」
只有本地记得住——DataHub 那边只有边本身，没有来源。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class LineagePackage(Base):
    """一次上传的 SQL 代码包及其扫描统计。"""

    __tablename__ = "lineage_packages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    domain_context_id: Mapped[str] = mapped_column(
        ForeignKey("domain_contexts.id"), index=True
    )
    name: Mapped[str] = mapped_column(String(255))
    #: scan（上传的代码包）/ manual（画布手工连的一批边）。两者共用这张表留档，
    #: 因为"这个域的血缘是谁补的"要能一起查；前端的代码包列表只列 scan。
    kind: Mapped[str] = mapped_column(String(16), default="scan", index=True)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    #: 解析用的 SQL 方言（mysql/postgres/hive/…）。重扫时可以换。
    dialect: Mapped[str] = mapped_column(String(32), default="mysql")
    #: 归档在服务端的落盘路径——重扫要用原包，不能只留统计。
    archive_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    sql_files: Mapped[int] = mapped_column(Integer, default=0)
    directories: Mapped[int] = mapped_column(Integer, default=0)
    statements: Mapped[int] = mapped_column(Integer, default=0)
    parsed_files: Mapped[int] = mapped_column(Integer, default=0)
    #: 解析失败的文件清单 [{"file": ..., "reason": ...}]。野包里一定有，藏起来就是骗人。
    failures_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: scanned（未上报）/ applied（全量上报）/ partial（只上报了一部分）
    status: Mapped[str] = mapped_column(String(20), default="scanned", index=True)
    applied_edges: Mapped[int] = mapped_column(Integer, default=0)
    #: 上报当时有多少张表因此脱离孤岛——事后 DataHub 里看不出来，只能当时记。
    applied_resolved: Mapped[int] = mapped_column(Integer, default=0)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    uploaded_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    scanned_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    edges: Mapped[list["LineagePackageEdge"]] = relationship(
        back_populates="package",
        cascade="all, delete-orphan",
        order_by="LineagePackageEdge.id",
    )


class LineagePackageEdge(Base):
    """代码包里解析出的一条血缘边（上游表 → 落点表 + 一对关联键）。"""

    __tablename__ = "lineage_package_edges"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    package_id: Mapped[str] = mapped_column(
        ForeignKey("lineage_packages.id"), index=True
    )

    source_table: Mapped[str] = mapped_column(String(512))
    target_table: Mapped[str] = mapped_column(String(512), index=True)
    #: 关联键的人话形态（``a.x = b.y``）。空＝这条边只有表级，喂不了关系推断。
    join_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    source_file: Mapped[str] = mapped_column(String(1024))

    #: 解析时对上的 DataHub URN。对不上则为空，边落到 blocked。
    source_urn: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    target_urn: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    #: ok（可上报）/ blocked（表名对不上 DataHub）/ skipped（不在本域）
    state: Mapped[str] = mapped_column(String(20), default="ok", index=True)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)

    #: 已写进 DataHub 的边。重复上报幂等，但本地要知道哪些已经报过。
    applied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    package: Mapped[LineagePackage] = relationship(back_populates="edges")
