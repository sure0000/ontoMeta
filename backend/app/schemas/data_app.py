"""数据应用（Data App）Pydantic schemas。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# --------------------------------------------------------------------- DataSource


class DataSourceCreate(BaseModel):
    name: str
    kind: str = "mock"  # postgres/mysql/duckdb/http/mock
    dsn_secret_ref: str | None = None


class DataSourceUpdate(BaseModel):
    name: str | None = None
    kind: str | None = None
    dsn_secret_ref: str | None = None


class DataSourceOut(BaseModel):
    id: str
    name: str
    kind: str
    status: str
    tested_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ------------------------------------------------------------------------ Binding


class DataAppBindingRef(BaseModel):
    kind: str  # object_type / property / business_logic
    id: str | None = None
    name: str | None = None
    display_name: str | None = None


class DataAppMeasure(BaseModel):
    ref: DataAppBindingRef
    agg: str = "sum"  # sum / count / avg / max / min


class DataAppFilter(BaseModel):
    ref: DataAppBindingRef
    op: str = "eq"  # eq / ne / gt / lt / ge / le / like
    value: Any | None = None


class DataAppTimeRange(BaseModel):
    ref: DataAppBindingRef | None = None
    window: str | None = None  # last_7d / last_30d / today / this_month …


class DataAppBinding(BaseModel):
    """数据集口径绑定。直接复用 Chat BI 口径拆解的结构骨架。"""

    primary_object_type_id: str | None = None
    measures: list[DataAppMeasure] = Field(default_factory=list)
    dimensions: list[DataAppBindingRef] = Field(default_factory=list)
    filters: list[DataAppFilter] = Field(default_factory=list)
    time_range: DataAppTimeRange | None = None
    row_limit: int = 100


# ------------------------------------------------------------------------ Dataset


class DataAppDatasetInput(BaseModel):
    id: str | None = None
    name: str = "数据集"
    primary_object_type_id: str | None = None
    binding: DataAppBinding = Field(default_factory=DataAppBinding)
    data_source_id: str | None = None


class DataAppDatasetOut(BaseModel):
    id: str
    app_id: str
    name: str
    primary_object_type_id: str | None = None
    binding: DataAppBinding = Field(default_factory=DataAppBinding)
    compiled_sql: str | None = None
    data_source_id: str | None = None
    created_at: datetime
    updated_at: datetime


# --------------------------------------------------------------------------- App


class DataAppCreate(BaseModel):
    domain_id: str
    app_type: str  # data_table / screen
    name: str | None = None
    description: str | None = None
    source: str = "manual"
    spec: dict[str, Any] | None = None
    datasets: list[DataAppDatasetInput] = Field(default_factory=list)


class DataAppUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    spec: dict[str, Any] | None = None
    datasets: list[DataAppDatasetInput] | None = None


class DataAppSummary(BaseModel):
    id: str
    domain_id: str
    app_type: str
    name: str
    description: str | None = None
    status: str
    source: str
    current_version: int
    published_version: int | None = None
    published_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class DataAppDetail(DataAppSummary):
    ontology_id: str | None = None
    spec: dict[str, Any] | None = None
    datasets: list[DataAppDatasetOut] = Field(default_factory=list)


# ------------------------------------------------------------------- Compile/Preview


class DataAppCompileResult(BaseModel):
    dataset_id: str | None = None
    compiled_sql: str | None = None
    grounded: bool = True
    warnings: list[str] = Field(default_factory=list)


class DataAppColumn(BaseModel):
    key: str
    title: str


class DataAppPreviewResult(BaseModel):
    dataset_id: str | None = None
    compiled_sql: str | None = None
    columns: list[DataAppColumn] = Field(default_factory=list)
    rows: list[dict[str, Any]] = Field(default_factory=list)
    used_mock: bool = True
    warnings: list[str] = Field(default_factory=list)


# ------------------------------------------------------------------------ Publish


class DataAppPublishRequest(BaseModel):
    version_comment: str | None = None
    operator: str | None = None


class DataAppVersionOut(BaseModel):
    id: str
    app_id: str
    version: int
    diff_summary: str | None = None
    operator: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


# --------------------------------------------------------- Chat BI → generate app


class GenerateAppFromChatRequest(BaseModel):
    domain_id: str
    app_type: str  # data_table / screen
    question: str
    conversation_id: str | None = None
    name: str | None = None
