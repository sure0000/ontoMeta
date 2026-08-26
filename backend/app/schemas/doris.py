"""Doris warehouse settings API contracts."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class DorisWarehouseConfigUpdate(BaseModel):
    warehouse_datasource_id: str
    enabled: bool = True
    query_host: str | None = None
    query_port: int = Field(9030, ge=1, le=65535)
    default_catalog: str = "internal"
    default_database: str | None = None
    connect_timeout_seconds: int = Field(10, ge=1, le=300)
    query_timeout_seconds: int = Field(15, ge=1, le=3600)
    ssl_enabled: bool = False
    fenodes: list[str] = Field(default_factory=list)
    # BE HTTP 地址（host:8040）。留空＝由 FE 告诉连接器 BE 在哪。
    benodes: list[str] = Field(default_factory=list)
    airflow_ddl_conn_id: str | None = None
    airflow_etl_conn_id: str | None = None
    airflow_flink_conn_id: str | None = None
    # Write-only; blank keeps the managed value.
    reader_dsn_secret_ref: str | None = None


class DorisWarehouseConfigOut(BaseModel):
    id: str
    warehouse_datasource_id: str
    enabled: bool
    query_host: str | None = None
    query_port: int
    default_catalog: str
    default_database: str | None = None
    connect_timeout_seconds: int
    query_timeout_seconds: int
    ssl_enabled: bool
    fenodes: list[str] = Field(default_factory=list)
    # BE HTTP 地址（host:8040）。留空＝由 FE 告诉连接器 BE 在哪。
    benodes: list[str] = Field(default_factory=list)
    airflow_ddl_conn_id: str | None = None
    airflow_etl_conn_id: str | None = None
    airflow_flink_conn_id: str | None = None
    reader_dsn_set: bool = False
    reader_dsn_hint: str | None = None
    created_at: Any
    updated_at: Any
