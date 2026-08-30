from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class DorisDeploymentPrepareInput(BaseModel):
    datasource_id: str
    database_prefix: str | None = None
    database_overrides: dict[str, str] = Field(default_factory=dict)
    table_overrides: dict[str, str] = Field(default_factory=dict)


class WarehouseMigrationBatchCreate(BaseModel):
    ontology_id: str
    approver: str = Field(min_length=1)
    rollback_owner: str = Field(min_length=1)
    observation_window_minutes: int = Field(gt=0)
    legacy_dag_ids: list[str] = Field(default_factory=list)
    new_dag_ids: list[str] = Field(default_factory=list)


class WarehouseMigrationStepInput(BaseModel):
    step: int = Field(ge=1, le=15)
    passed: bool
    report: dict[str, Any] = Field(default_factory=dict)
    artifact_ids: list[str] = Field(default_factory=list)


class WarehouseMigrationApprovalInput(BaseModel):
    approver: str = Field(min_length=1)
    note: str = Field(min_length=1)


class WarehouseMigrationOperatorInput(BaseModel):
    pass


class WarehouseMigrationRollbackInput(BaseModel):
    reason: str = Field(min_length=1)


class WarehouseRollbackDrillInput(BaseModel):
    report: dict[str, Any]


class ShadowDifferenceInput(BaseModel):
    cases: list[dict[str, Any]] = Field(min_length=1)


class IngestionContractInput(BaseModel):
    object_type_id: str
    source_datasource_id: str
    source_physical_table: str
    source_mapping: dict[str, str] = Field(default_factory=dict)
    doris_datasource_id: str
    target_ods_database: str
    mode: str = "full"
    primary_keys: list[str] = Field(default_factory=list)
    sequence_column: str | None = None
    incremental_column: str | None = None
    initial_watermark: str | None = None
    late_arrival_policy: str = "strict"
    idempotency_strategy: str = "primary_key_upsert"
    delete_policy: str = "ignore"
    refresh_cron: str | None = None
    flink_params: dict[str, Any] = Field(default_factory=dict)
    status: str = "draft"


class IngestionTaskResultInput(BaseModel):
    task_state: str
    result: dict[str, Any] = Field(default_factory=dict)


class IngestionContractOut(IngestionContractInput):
    target_ods_table: str
    id: str
    ontology_id: str
    ontology_version: int
    last_success_at: datetime | None = None
    sync_watermark: str | None = None
    flink_job_id: str | None = None
    checkpoint_path: str | None = None
    savepoint_path: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


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


class MaterializeRequest(BaseModel):
    """本体一键物化请求：把当前工作本体（草稿或已发布）落到某个目标数据源。"""

    target_datasource_id: str = Field(description="目标存储（DataSource）id，其 dsn 即落库连接串")
    engine: str = Field("doris", description="目标数仓引擎（固定为 Doris）")
    database_prefix: str | None = Field(
        None, description="库名后缀，如 erp → dim_erp；被 database_overrides 命中的层不受其影响"
    )
    database_overrides: dict[str, str] = Field(
        default_factory=dict,
        description="{分层: 目标库名}，人工指定该层落到哪个库；缺省按「层[_前缀]」生成",
    )
    table_overrides: dict[str, str] = Field(
        default_factory=dict,
        description="{contract_id: 物理表名}，人工指定的表名；缺省用实体技术名",
    )
    selected_targets: list[str] | None = Field(
        None, description="勾选要物化的实体名；空/None = 全部可物化实体"
    )
    overrides: dict[str, dict] = Field(
        default_factory=dict,
        description="{contract_id: {字段: 值}} 人工覆盖的存储策略/层/表名等，写回契约并钉住",
    )
    intent: str | None = None
    operator: str | None = None


class MaterializeResult(BaseModel):
    """物化执行回执。``receipt`` 内含 DDL 执行批次与逐表状态。"""

    artifact_id: str
    status: str
    ok: bool
    name: str
    receipt: dict | None = None
    executed_at: datetime | None = None
    operator: str | None = None
    created_at: datetime | None = None


class MaterializePreflightRequest(BaseModel):
    """提交前自检请求：只需目标存储与勾选范围，不写回、不触发。"""

    target_datasource_id: str = Field(description="目标存储（DataSource）id")
    engine: str = Field("doris", description="目标数仓引擎（固定为 Doris）")
    selected_targets: list[str] | None = Field(
        None, description="勾选要物化的实体名；空/None = 全部可物化实体（用于批次规模预警）"
    )


class PreflightItemOut(BaseModel):
    """单项自检结果。``next_step`` 是失败时可照做的下一步。"""

    key: str
    label: str
    status: str = Field(description="pass / warn / fail")
    blocking: bool = Field(description="为真且 fail 时应禁用提交")
    detail: str
    next_step: str | None = None


class MaterializePreflightResult(BaseModel):
    ok: bool = Field(description="无阻断失败即可提交（提醒项不拦）")
    items: list[PreflightItemOut] = Field(default_factory=list)
