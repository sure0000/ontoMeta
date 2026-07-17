from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.schemas.ontology import ObjectTypeSummary, PropertyOut, VersionRecordOut

class BusinessLogicObjectBindingOut(BaseModel):
    id: str
    business_logic_id: str
    object_type_id: str
    object_type_name: str | None = None
    object_type_display_name: str | None = None
    role: str
    source: str
    confidence: float | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class BusinessLogicPropertyBindingOut(BaseModel):
    id: str
    business_logic_id: str
    property_id: str
    property_name: str | None = None
    property_display_name: str | None = None
    object_type_id: str | None = None
    object_type_name: str | None = None
    role: str
    source: str
    confidence: float | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class BusinessLogicObjectBindingCreate(BaseModel):
    business_logic_id: str
    object_type_id: str
    role: str = "subject"
    operator: str | None = None


class BusinessLogicPropertyBindingCreate(BaseModel):
    business_logic_id: str
    property_id: str
    role: str = "input"
    operator: str | None = None


class BusinessLogicPropertyOption(BaseModel):
    """业务逻辑编辑器中可挑选的已发布字段候选(含所属对象信息)。"""

    property_id: str
    property_name: str
    property_display_name: str | None = None
    object_type_id: str
    object_type_name: str
    object_type_display_name: str | None = None

    model_config = {"from_attributes": True}


class BusinessLogicCategoryOut(BaseModel):
    id: str
    name: str
    description: str | None = None
    logic_count: int = 0
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class BusinessLogicCategoryCreate(BaseModel):
    name: str
    description: str | None = None


class BusinessLogicCategoryUpdate(BaseModel):
    name: str | None = None
    description: str | None = None


class BusinessLogicCreate(BaseModel):
    domain_id: str
    name: str
    display_name: str
    logic_type: str
    description: str | None = None
    expression_summary: str | None = None
    expression_draft: dict | None = None
    expression_json: dict | None = None
    category_id: str | None = None
    operator: str | None = None


class BusinessLogicUpdate(BaseModel):
    display_name: str | None = None
    description: str | None = None
    logic_type: str | None = None
    expression_summary: str | None = None
    expression_draft: dict | None = None
    expression_json: dict | None = None
    category_id: str | None = None
    operator: str | None = None


class BusinessLogicImportRequest(BaseModel):
    domain_id: str
    code: str
    source_type: str = "sql"
    category_id: str | None = None
    operator: str | None = None


class ExpressionFormatRequest(BaseModel):
    domain_id: str
    expression_draft: dict
    logic_type: str | None = None
    description: str | None = None


class ExpressionFormatResponse(BaseModel):
    expression_json: dict
    expression_summary: str


class BusinessLogicOut(BaseModel):
    id: str
    name: str
    display_name: str
    logic_type: str
    description: str | None = None
    expression_summary: str | None = None
    expression_draft: dict | None = None
    expression_json: dict | None = None
    source_type: str | None = None
    source_ref: str | None = None
    status: str
    source_confidence: float | None = None
    domain_context_id: str | None = None
    domain_name: str | None = None
    category_id: str | None = None
    category_name: str | None = None
    bound_object_count: int = 0
    bound_property_count: int = 0
    updated_at: datetime

    model_config = {"from_attributes": True}


class BusinessLogicRef(BaseModel):
    """关联对象上绑定的业务逻辑简要引用。"""

    id: str
    name: str
    display_name: str
    logic_type: str
    status: str

    model_config = {"from_attributes": True}


class BusinessLogicDetail(BusinessLogicOut):
    related_object_types: list[ObjectTypeSummary] = Field(default_factory=list)
    related_object_logics: dict[str, list[BusinessLogicRef]] = Field(default_factory=dict)
    related_properties: list[PropertyOut] = Field(default_factory=list)
    object_bindings: list[BusinessLogicObjectBindingOut] = Field(default_factory=list)
    property_bindings: list[BusinessLogicPropertyBindingOut] = Field(default_factory=list)
    version_records: list["VersionRecordOut"] = Field(default_factory=list)
    ontology_id: str | None = None
    available_object_types: list[ObjectTypeSummary] = Field(default_factory=list)
    available_properties: list[BusinessLogicPropertyOption] = Field(default_factory=list)
