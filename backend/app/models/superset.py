"""Superset 资产登记簿。

**这张表不是真源**：图表和看板本身住在 Superset 里，那边才是权威。ontoMeta 只记
"我们让 Agent 建了哪些东西、它们指向哪个落点、点哪个链接能打开"，用来在平台内列出
资产、做嵌入、以及回答"这张图是谁按什么口径建的"。

因此这里刻意不存 Superset 的 ``params``/``query_context`` 副本：存了就会与 Superset
里被人手改过的版本分叉，而分叉之后没人知道该信哪一份。要看图长什么样，去 Superset。

``state`` 由显式对账写入（列一遍 Superset 的 chart/dashboard，对不上的置 ``missing``），
绝不拿本地行冒充存在性——那正是"平台上列着、点进去 404"的由来。
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

#: 资产类型。dataset 也登记：它是图表的前置，且"这个落点有没有推给 Superset"本身
#: 就是个要能回答的问题。
ASSET_TYPES = ("dataset", "chart", "dashboard")

#: 对账状态。``unknown`` = 还没对过账，与 ``active`` 区分开——没对过不等于在。
ASSET_STATES = ("unknown", "active", "missing")


def _uuid() -> str:
    return str(uuid.uuid4())


class SupersetAsset(Base):
    __tablename__ = "superset_assets"
    __table_args__ = (
        # 同一个 Superset 对象只登记一次；重复登记会让"这张图是谁建的"出现两个答案。
        UniqueConstraint("asset_type", "superset_id", name="uq_superset_assets_type_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    asset_type: Mapped[str] = mapped_column(String(16), index=True)
    superset_id: Mapped[int] = mapped_column(Integer, index=True)

    title: Mapped[str] = mapped_column(String(255))
    #: Superset 内的相对路径（如 ``/explore/?slice_id=12``）。绝对地址由运行期配置的
    #: ``public_base_url`` 拼——后端访问地址与用户访问地址是两回事，存死会在某一端断掉。
    url_path: Mapped[str] = mapped_column(String(512), default="", server_default="")
    viz_type: Mapped[str | None] = mapped_column(String(64), nullable=True)

    #: 这张图建在哪个落点上（``obj:<id>@serving`` / ``logic:<id>@ads``）。
    #: 是登记簿里唯一指回本体治理的线索，故 dataset/chart 都尽量填。
    dataset_ref: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    #: 图表所依赖的 Superset dataset id（chart 才有），便于反查同源图表。
    superset_dataset_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    domain_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("domain_contexts.id"), nullable=True, index=True
    )
    ontology_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)

    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: mcp / web —— 分清是 Agent 建的还是人在平台上点的。
    created_via: Mapped[str] = mapped_column(String(16), default="mcp", server_default="mcp")

    state: Mapped[str] = mapped_column(String(16), default="unknown", server_default="unknown")
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    extra_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
