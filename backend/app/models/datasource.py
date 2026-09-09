"""数据源与 Doris 数仓配置的 ORM 模型。

``DataSource`` 是全平台的「连哪些库」登记表——物化、搬运、Agent 取数都从这里选源；
``DorisWarehouseConfig`` 是默认数仓那一条的额外配置。

自研数据应用（DataApp/Panel/版本/公开分享）已整体下线，图表与看板改由 Apache Superset
承载，本平台只留一张登记簿（见 ``models/superset.py``）。
JSON 字段统一以 Text 存储（json.dumps），保证 SQLite / PostgreSQL 一致。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class DataSource(Base):
    """物理数据源连接。

    ``purpose`` 是路由事实源：业务源和数仓连接不能再通过 catalog_name
    推断。``catalog_name`` 仅作为外部 catalog 元数据保留。
    """

    __tablename__ = "data_sources"
    __table_args__ = (
        Index(
            "uq_data_sources_default_warehouse",
            "is_default_warehouse",
            unique=True,
            sqlite_where=text("is_default_warehouse = 1"),
            postgresql_where=text("is_default_warehouse = true"),
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(50))  # postgres/mysql/duckdb/http/mock/doris
    purpose: Mapped[str] = mapped_column(String(30), default="business_source", server_default="business_source")
    is_default_warehouse: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    # DSN / 密钥仅存引用或加密串，阶段 1 允许为空
    dsn_secret_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 物理映射：{"tables":{ontologyName:physical}, "columns":{...}}，Text(json)
    # 收敛到 StarRocks 多目录后，物理表名为 catalog.db.table 三段式
    mapping_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # StarRocks 多目录架构：NULL/"internal"=warehouse，其他值=源库 catalog 名（如"erp"/"crm"）
    catalog_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # 这个数据源在 Superset 里对应的 database（连接）编号。首次建数据集时由
    # ``services/superset_database.resolve_database_id`` 按 host/port 比对写入并缓存；
    # 也可以直接 PATCH 覆盖，作为自动匹配认不出来时的人工兜底。
    # 是「连接」不是「库」——库由落点的 ``库.表`` 拆出来单独传给 Superset。
    superset_database_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="untested")
    tested_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class DorisWarehouseConfig(Base):
    """Default Doris connection contract, one-to-one with the default DataSource.

    Credentials remain in managed secrets/Airflow connections; this table stores
    endpoints and stable connection aliases only.
    """

    __tablename__ = "doris_warehouse_configs"
    __table_args__ = (UniqueConstraint("warehouse_datasource_id", name="uq_doris_config_datasource"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default="default")
    warehouse_datasource_id: Mapped[str] = mapped_column(ForeignKey("data_sources.id"))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    query_host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    query_port: Mapped[int] = mapped_column(Integer, default=9030, server_default="9030")
    default_catalog: Mapped[str] = mapped_column(String(100), default="internal", server_default="internal")
    default_database: Mapped[str | None] = mapped_column(String(255), nullable=True)
    connect_timeout_seconds: Mapped[int] = mapped_column(Integer, default=10, server_default="10")
    query_timeout_seconds: Mapped[int] = mapped_column(Integer, default=15, server_default="15")
    ssl_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    fenodes_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # BE 的 HTTP 地址（``host:8040``）。留空＝由 FE 告诉 Flink 连接器 BE 在哪，这在
    # 单机/容器化 Doris 上会拿到 BE 自报的 127.0.0.1，集群外的 Flink 连不上
    # （``Connect to 127.0.0.1:8040 failed``，报在作业运行期而非提交期）。填了就直接
    # 下发给连接器的 benodes，不再问 FE。
    benodes_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    airflow_ddl_conn_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    airflow_etl_conn_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    airflow_flink_conn_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reader_dsn_secret_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=True)
