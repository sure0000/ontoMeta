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


class DatasetInput(BaseModel):
    urn: str
    name: str
    display_name: str | None = None
    description: str | None = None
    platform: str | None = None
    container: str | None = None
    fields: list[FieldInput] = Field(default_factory=list)


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
    latest_ontology_id: str | None = None
    latest_ontology_status: str | None = None
    published_ontology_id: str | None = None
    published_ontology_version: int | None = None


class DraftProgressOut(BaseModel):
    task_id: str
    status: str
    progress: int
    message: str | None = None
    ontology_id: str | None = None


class TaskRecordOut(BaseModel):
    id: str
    status: str
    progress: int
    message: str | None = None
    error_summary: str | None = None
    ontology_id: str | None = None
    evidence_count: int = 0
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
