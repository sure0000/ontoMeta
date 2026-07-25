"""数据应用（Data App）ORM 模型。

数据应用 = 在已发布本体语义层之上、由「本体查询数据集」驱动的可视化产物：
- data_table：数据表格页面
- screen：可视化大屏

沿用项目既有范式：status + version + JSON spec + 溯源 + 二次确认发布。
JSON 字段统一以 Text 存储（json.dumps），保证 SQLite / PostgreSQL 一致。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class DataSource(Base):
    """物理数据源连接（阶段 1 可为空，preview 走 Mock 造数）。"""

    __tablename__ = "data_sources"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(50))  # postgres/mysql/duckdb/http/mock
    # DSN / 密钥仅存引用或加密串，阶段 1 允许为空
    dsn_secret_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="untested")
    tested_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class DataApp(Base):
    """数据应用聚合根。"""

    __tablename__ = "data_apps"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    domain_id: Mapped[str] = mapped_column(
        ForeignKey("domain_contexts.id"), index=True
    )
    # 绑定的已发布本体（保存/发布时落地，供 grounding 与溯源）
    ontology_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    app_type: Mapped[str] = mapped_column(String(30))  # data_table / screen
    name: Mapped[str] = mapped_column(String(255), default="未命名应用")
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="draft")
    # chat_generated / manual
    source: Mapped[str] = mapped_column(String(30), default="manual")
    # 前端渲染契约（layout / widgets / columns …），Text(json)
    spec_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    current_version: Mapped[int] = mapped_column(Integer, default=1)
    published_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    datasets: Mapped[list["DataAppDataset"]] = relationship(
        back_populates="app",
        cascade="all, delete-orphan",
    )
    versions: Mapped[list["DataAppVersion"]] = relationship(
        back_populates="app",
        cascade="all, delete-orphan",
    )


class DataAppDataset(Base):
    """应用数据集 = 一次可执行的本体查询（口径绑定 → 编译 SQL）。"""

    __tablename__ = "data_app_datasets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    app_id: Mapped[str] = mapped_column(
        ForeignKey("data_apps.id"), index=True
    )
    name: Mapped[str] = mapped_column(String(255), default="数据集")
    primary_object_type_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # 口径拆解落地：measures/dimensions/filters/time_range，元素含本体引用
    binding_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    compiled_sql: Mapped[str | None] = mapped_column(Text, nullable=True)
    data_source_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    app: Mapped["DataApp"] = relationship(back_populates="datasets")


class DataAppVersion(Base):
    """发布快照：冻结 spec + 数据集绑定 + 编译 SQL，可只读回看/回滚。"""

    __tablename__ = "data_app_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    app_id: Mapped[str] = mapped_column(
        ForeignKey("data_apps.id"), index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    spec_snapshot_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    datasets_snapshot_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    diff_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    operator: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    app: Mapped["DataApp"] = relationship(back_populates="versions")
