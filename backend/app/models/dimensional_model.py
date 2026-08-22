"""维度模型制品：星型/雪花模型的显式设计与验证。"""
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, Column, DateTime, Enum, ForeignKey, String, Text, Integer
from sqlalchemy.orm import relationship

from app.database import Base


class DimensionalModel(Base):
    """维度模型：一个完整的星型或雪花模型设计。
    
    区别于物化契约的地方：
    - 物化契约是"如何把本体投影到物理表"
    - 维度模型是"业务过程、粒度、事实、维度的显式设计"
    
    一个维度模型可以编译为多个物化契约。
    """
    __tablename__ = "dimensional_models"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    modeling_case_id = Column(String(36), ForeignKey("modeling_cases.id"), nullable=True, index=True)
    domain_id = Column(String(36), ForeignKey("domain_contexts.id"), nullable=False, index=True)
    ontology_id = Column(String(36), ForeignKey("ontologies.id"), nullable=False, index=True)
    
    name = Column(String(200), nullable=False, comment="模型名称，如：订单分析星型模型")
    display_name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    
    # 核心设计
    business_process = Column(Text, nullable=False, comment="业务过程描述")
    grain = Column(Text, nullable=False, comment="粒度声明，如：每笔订单明细行")
    
    # 事实表设计
    fact_tables = Column(JSON, nullable=False, default=list, comment="""
    [
      {
        "name": "fct_order_line",
        "display_name": "订单明细事实",
        "source_object_id": "...",
        "measures": [
          {"name": "quantity", "field": "quantity", "additive_type": "additive"},
          {"name": "amount", "field": "amount", "additive_type": "additive"},
          {"name": "unit_price", "field": "unit_price", "additive_type": "non_additive"}
        ],
        "dimension_keys": ["customer_key", "product_key", "order_date_key", "channel_key"],
        "degenerate_dimensions": ["order_id", "line_number"]
      }
    ]
    """)
    
    # 维度设计
    dimensions = Column(JSON, nullable=False, default=list, comment="""
    [
      {
        "name": "dim_customer",
        "display_name": "客户维度",
        "source_object_id": "...",
        "natural_key": ["customer_code"],
        "surrogate_key": "customer_key",
        "scd_type": "scd2",
        "scd_config": {
          "effective_date": "valid_from",
          "expiration_date": "valid_to",
          "current_flag": "is_current"
        },
        "attributes": [
          {"name": "customer_name", "field": "name"},
          {"name": "customer_level", "field": "level"},
          {"name": "region", "field": "region"}
        ],
        "role_playing": null
      },
      {
        "name": "dim_date",
        "display_name": "日期维度",
        "source_object_id": null,
        "natural_key": ["date"],
        "surrogate_key": "date_key",
        "scd_type": "none",
        "attributes": [
          {"name": "year", "derived": true},
          {"name": "quarter", "derived": true},
          {"name": "month", "derived": true},
          {"name": "week", "derived": true}
        ],
        "is_date_dimension": true
      }
    ]
    """)
    
    # 一致性维度
    conformed_dimensions = Column(JSON, nullable=False, default=list, comment="""
    [
      {
        "dimension_name": "dim_customer",
        "shared_across_facts": ["fct_order_line", "fct_invoice"],
        "description": "跨订单和发票的一致客户维度"
      }
    ]
    """)
    
    # 模型类型
    model_type = Column(
        Enum("star", "snowflake", "constellation", name="dimensional_model_type"),
        nullable=False,
        default="star",
        comment="星型/雪花/星座模型"
    )
    
    # 验证结果
    validation_issues = Column(JSON, nullable=True, comment="粒度冲突、扇出风险等验证问题")
    
    # 编译状态
    compiled_contracts = Column(JSON, nullable=True, comment="编译生成的物化契约 ID 列表")
    compiled_at = Column(DateTime(timezone=True), nullable=True)
    
    # 状态
    status = Column(
        Enum("draft", "validated", "confirmed", "compiled", "deployed", name="dimensional_model_status"),
        nullable=False,
        default="draft"
    )
    
    # 版本
    version = Column(Integer, nullable=False, default=1)
    
    # 元数据
    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    
    # 关系
    modeling_case = relationship("ModelingCase", back_populates="dimensional_models")
    domain = relationship("DomainContext")
    ontology = relationship("Ontology")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "modeling_case_id": self.modeling_case_id,
            "domain_id": self.domain_id,
            "ontology_id": self.ontology_id,
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "business_process": self.business_process,
            "grain": self.grain,
            "fact_tables": self.fact_tables,
            "dimensions": self.dimensions,
            "conformed_dimensions": self.conformed_dimensions,
            "model_type": self.model_type,
            "validation_issues": self.validation_issues,
            "compiled_contracts": self.compiled_contracts,
            "compiled_at": self.compiled_at.isoformat() if self.compiled_at else None,
            "status": self.status,
            "version": self.version,
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
