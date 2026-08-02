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


class MaterializeRequest(BaseModel):
    """本体一键物化请求：把当前工作本体（草稿或已发布）落到某个目标数据源。"""

    target_datasource_id: str = Field(description="目标存储（DataSource）id，其 dsn 即落库连接串")
    engine: str = Field("hive", description="目标数仓引擎（决定 DDL/ETL 方言）")
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
    load_strategy: str | None = Field(
        None,
        description="同步方式：full/incremental/cdc；写回选中契约并影响 ETL。空=沿用契约既定策略",
    )
    selected_targets: list[str] | None = Field(
        None, description="勾选要物化的实体名；空/None = 全部可物化实体"
    )
    overrides: dict[str, dict] = Field(
        default_factory=dict,
        description="{contract_id: {字段: 值}} 人工覆盖的存储策略/层/表名等，写回契约并钉住",
    )
    execute_mode: str | None = Field(
        None,
        description="orchestrated=交 Airflow 编排（默认）；direct=ontoMeta 直连落库（开发模式）",
    )
    intent: str | None = None
    operator: str | None = None


class MaterializeResult(BaseModel):
    """物化执行回执。``receipt`` 内含 ddl/etl 分阶段结果与逐表状态。"""

    artifact_id: str
    status: str
    ok: bool
    name: str
    receipt: dict | None = None
    executed_at: datetime | None = None
    operator: str | None = None
    created_at: datetime | None = None

