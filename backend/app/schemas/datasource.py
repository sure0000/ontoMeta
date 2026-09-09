"""数据应用（Data App）Pydantic schemas。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel

# --------------------------------------------------------------------- DataSource


class DataSourceCreate(BaseModel):
    name: str
    kind: str = "mock"  # postgres/mysql/duckdb/sqlite/http/mock/doris
    purpose: str = "business_source"  # business_source / warehouse
    is_default_warehouse: bool = False
    enabled: bool = True
    dsn_secret_ref: str | None = None
    mapping: dict[str, Any] | None = None
    # StarRocks 多目录：NULL/"internal"=warehouse；其他值=源库 catalog 名
    catalog_name: str | None = None


class DataSourceUpdate(BaseModel):
    name: str | None = None
    kind: str | None = None
    purpose: str | None = None
    is_default_warehouse: bool | None = None
    enabled: bool | None = None
    dsn_secret_ref: str | None = None
    mapping: dict[str, Any] | None = None
    catalog_name: str | None = None
    # 人工兜底：自动匹配认不出 Superset 里对应的 database 时，直接把编号填进来。
    superset_database_id: int | None = None


class DataSourceOut(BaseModel):
    id: str
    name: str
    kind: str
    purpose: str = "business_source"
    is_default_warehouse: bool = False
    enabled: bool = True
    status: str
    mapping: dict[str, Any] | None = None
    tested_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    # StarRocks 多目录：NULL/"internal"=warehouse，其他值=源库 catalog 名
    catalog_name: str | None = None
    # 这个数据源在 Superset 里的 database（连接）编号；建数据集时自动解析并缓存。
    superset_database_id: int | None = None
    # 连接信息回显：只返回 password_set/password_hint，不返回密码明文；dsn 整体仍不下发。
    dsn_set: bool = False
    host: str | None = None
    port: int | None = None
    database: str | None = None
    username: str | None = None
    password: str | None = None  # 始终为 None；密码只保存在受管 secret/DSN 引用中
    password_set: bool = False
    password_hint: str | None = None
    path: str | None = None  # 文件类（sqlite/duckdb）的文件路径

    model_config = {"from_attributes": True}


# ------------------------------------------------------------------------ Binding
