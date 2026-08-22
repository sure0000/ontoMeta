from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

class DomainInput(BaseModel):
    id: str
    name: str
    description: str | None = None
    owner: str | None = None


class FieldInput(BaseModel):
    name: str
    display_name: str | None = None
    description: str | None = None
    data_type: str | None = None
    is_primary_key: bool = False
    is_foreign_key: bool = False
    foreign_key_target: str | None = None
    sample_values: list[str] = Field(default_factory=list)
    # profiling 统计：该字段的不同值个数（未开启 profiling 时为 None）。
    unique_count: int | None = None


class DatasetInput(BaseModel):
    urn: str
    name: str
    display_name: str | None = None
    description: str | None = None
    platform: str | None = None
    container: str | None = None
    fields: list[FieldInput] = Field(default_factory=list)
    # profiling 的总行数（未开启时 None），供粒度/主键唯一度分析。
    row_count: int | None = None
    # 人工挂载的业务术语（GlossaryTerm 名称），已确认的业务概念信号。
    glossary_terms: list[str] = Field(default_factory=list)
    # DataHub 原生 subType（如 View/Table）与 tag 名称，供对象角色分类器识别系统/技术资产。
    subtypes: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class LineageInput(BaseModel):
    source_urn: str
    target_urn: str
    lineage_type: str = "table"


class LogicEvidenceInput(BaseModel):
    source_type: str
    source_ref: str
    name: str
    expression: str | None = None
    description: str | None = None


class DataHubDomainBundle(BaseModel):
    domain: DomainInput
    datasets: list[DatasetInput] = Field(default_factory=list)
    lineages: list[LineageInput] = Field(default_factory=list)
    logic_evidences: list[LogicEvidenceInput] = Field(default_factory=list)


class DataHubDatasetOption(BaseModel):
    """DataHub dataset 搜索结果（含已映射的 ObjectType 信息）。"""
    urn: str
    name: str
    display_name: str | None = None
    description: str | None = None
    platform: str | None = None
    container: str | None = None
    object_type_id: str | None = None
    object_type_display_name: str | None = None
    datahub_url: str | None = None


class EnsureObjectTypeRequest(BaseModel):
    ontology_id: str
    dataset_urn: str
    operator: str | None = None


class ManualPropertyInput(BaseModel):
    name: str
    display_name: str | None = None
    data_type: str | None = None
    semantic_type: str | None = None
    required: bool = False
    primary_key: bool = False


class ManualObjectCreateRequest(BaseModel):
    name: str
    display_name: str
    description: str | None = None
    dialect: str = "mysql"
    data_source: str | None = None
    properties: list[ManualPropertyInput] = []


class ManualObjectCreateResponse(BaseModel):
    ontology_id: str
    object_type_id: str
    table_name: str
    ddl: str


class DomainContextSummary(BaseModel):
    id: str
    datahub_domain_id: str
    name: str
    description: str | None = None
    owner: str | None = None
    status: str
    draft_count: int = 0
    published_count: int = 0
    object_type_count: int = 0
    relation_type_count: int = 0
    published_object_type_count: int = 0
    latest_draft_at: datetime | None = None
    latest_published_at: datetime | None = None
    updated_at: datetime

    model_config = {"from_attributes": True}


class DomainContextDetail(DomainContextSummary):
    datahub_url: str | None = None
    # 一域一本体：工作本体就是该域**唯一**那一行，既是草稿工作台也是发布载体。
    # 旧字段名 latest_ontology_id 取的是「按 updated_at 最新的那行」，会在 draft 与
    # published 两行之间来回跳——页面主体因此不稳定。现在没有第二行可跳。
    working_ontology_id: str | None = None
    working_ontology_status: str | None = None
    published_ontology_id: str | None = None
    published_ontology_version: int | None = None
    # 发布态指标（供域卡片与页头「待固化」提示条）：
    # 已发布内容改动后未固化的实体数 / 本次发布会新提升的实体数 /
    # 待复核业务对象数 / 未解决的字段级冲突数。
    unpublished_change_count: int = 0
    pending_publish_count: int = 0
    needs_review_count: int = 0
    unresolved_conflict_count: int = 0


class DraftProgressOut(BaseModel):
    task_id: str
    status: str
    progress: int
    message: str | None = None
    ontology_id: str | None = None
    scope: str = "full"


class TaskRecordOut(BaseModel):
    id: str
    status: str
    progress: int
    message: str | None = None
    error_summary: str | None = None
    ontology_id: str | None = None
    evidence_count: int = 0
    scope: str = "full"
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class DraftDuplicateReport(BaseModel):
    domain_id: str
    draft_count: int
    draft_ontology_ids: list[str] = Field(default_factory=list)
    will_purge_on_regenerate: bool = True
    message: str


class ChangeLogOut(BaseModel):
    id: str
    entity_type: str
    entity_id: str
    action: str
    operator: str | None = None
    change_summary: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}
