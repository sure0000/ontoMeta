from datetime import datetime

from pydantic import BaseModel, Field


class MaterializationContractOut(BaseModel):
    id: str
    ontology_id: str
    target_kind: str
    target_id: str
    # 便于前端展示，由服务层补齐（本体实体的技术名与业务名）。
    target_name: str | None = None
    target_display_name: str | None = None

    target_layer: str
    engines: list[str] = Field(default_factory=list)
    load_strategy: str
    partition_key: str | None = None
    scd_type: str
    refresh_cron: str | None = None
    materialized: bool
    derivation_reason: str | None = None

    origin: str
    pinned_fields: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class MaterializationContractUpdate(BaseModel):
    """人工编辑。仅提交的字段会被钉住（计入 overridden_fields），机器重推导时不再覆盖。"""

    target_layer: str | None = None
    engines: list[str] | None = None
    load_strategy: str | None = None
    partition_key: str | None = None
    scd_type: str | None = None
    refresh_cron: str | None = None
    materialized: bool | None = None


class MaterializationContractSyncResult(BaseModel):
    """机器推导默认值后的同步结果。"""

    ontology_id: str
    created: int
    updated: int
    skipped_pinned: int = Field(
        0, description="因人工钉住而未被覆盖的字段数（跨全部契约累计）"
    )
    total: int
