"""维度模型服务：设计、验证、编译维度模型。"""
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.models.dimensional_model import DimensionalModel
from app.models.modeling import ModelingCase


class DimensionalModelService:
    """维度模型服务。
    
    负责：
    1. 创建和管理维度模型
    2. 验证粒度一致性、扇出风险
    3. 编译为物化契约
    """
    
    def create_model(
        self,
        db: Session,
        *,
        modeling_case_id: str | None,
        domain_id: str,
        ontology_id: str,
        name: str,
        display_name: str,
        business_process: str,
        grain: str,
        fact_tables: list[dict],
        dimensions: list[dict],
        conformed_dimensions: list[dict] | None = None,
        model_type: str = "star",
        description: str | None = None,
        created_by: str | None = None,
    ) -> dict[str, Any]:
        """创建维度模型。"""
        model = DimensionalModel(
            id=str(uuid.uuid4()),
            modeling_case_id=modeling_case_id,
            domain_id=domain_id,
            ontology_id=ontology_id,
            name=name,
            display_name=display_name,
            description=description,
            business_process=business_process,
            grain=grain,
            fact_tables=fact_tables,
            dimensions=dimensions,
            conformed_dimensions=conformed_dimensions or [],
            model_type=model_type,
            status="draft",
            created_by=created_by,
        )
        db.add(model)
        db.commit()
        db.refresh(model)
        return model.to_dict()
    
    def get_model(self, db: Session, model_id: str) -> dict[str, Any] | None:
        """获取维度模型。"""
        model = db.query(DimensionalModel).filter(DimensionalModel.id == model_id).first()
        return model.to_dict() if model else None
    
    def list_models(
        self,
        db: Session,
        *,
        modeling_case_id: str | None = None,
        domain_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """列出维度模型。"""
        query = db.query(DimensionalModel)
        if modeling_case_id:
            query = query.filter(DimensionalModel.modeling_case_id == modeling_case_id)
        if domain_id:
            query = query.filter(DimensionalModel.domain_id == domain_id)
        if status:
            query = query.filter(DimensionalModel.status == status)
        models = query.order_by(DimensionalModel.created_at.desc()).limit(limit).all()
        return [m.to_dict() for m in models]
    
    def update_model(
        self,
        db: Session,
        model_id: str,
        updates: dict[str, Any],
    ) -> dict[str, Any]:
        """更新维度模型。"""
        model = db.query(DimensionalModel).filter(DimensionalModel.id == model_id).first()
        if not model:
            raise ValueError("维度模型不存在")
        
        allowed_fields = {
            "display_name", "description", "business_process", "grain",
            "fact_tables", "dimensions", "conformed_dimensions", "model_type"
        }
        
        for key, value in updates.items():
            if key in allowed_fields:
                setattr(model, key, value)
        
        model.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(model)
        return model.to_dict()
    
    def validate_model(self, db: Session, model_id: str) -> dict[str, Any]:
        """验证维度模型：检查粒度一致性、扇出风险等。"""
        model = db.query(DimensionalModel).filter(DimensionalModel.id == model_id).first()
        if not model:
            raise ValueError("维度模型不存在")
        
        issues = []
        
        # 1. 检查事实表是否定义了度量
        for fact in model.fact_tables:
            if not fact.get("measures"):
                issues.append({
                    "severity": "error",
                    "fact_table": fact.get("name"),
                    "message": "事实表未定义度量"
                })
            
            # 检查度量的可加性类型
            for measure in fact.get("measures", []):
                if not measure.get("additive_type"):
                    issues.append({
                        "severity": "warning",
                        "fact_table": fact.get("name"),
                        "measure": measure.get("name"),
                        "message": "度量未声明可加性类型"
                    })
        
        # 2. 检查维度是否有代理键
        for dim in model.dimensions:
            if not dim.get("surrogate_key"):
                issues.append({
                    "severity": "warning",
                    "dimension": dim.get("name"),
                    "message": "维度未定义代理键"
                })
            
            # 检查 SCD 配置
            if dim.get("scd_type") == "scd2":
                config = dim.get("scd_config", {})
                if not all([
                    config.get("effective_date"),
                    config.get("expiration_date"),
                    config.get("current_flag")
                ]):
                    issues.append({
                        "severity": "error",
                        "dimension": dim.get("name"),
                        "message": "SCD2 维度缺少有效日期、失效日期或当前标志配置"
                    })
        
        # 3. 检查粒度与事实表的一致性
        # 简化版：检查事实表的维度键是否都在 dimensions 中定义
        defined_dims = {d.get("name") for d in model.dimensions}
        for fact in model.fact_tables:
            for dim_key in fact.get("dimension_keys", []):
                # 提取维度名（假设 key 名为 {dim_name}_key）
                dim_name = dim_key.replace("_key", "")
                if f"dim_{dim_name}" not in defined_dims and dim_name not in defined_dims:
                    issues.append({
                        "severity": "warning",
                        "fact_table": fact.get("name"),
                        "dimension_key": dim_key,
                        "message": f"事实表引用的维度键 {dim_key} 在维度列表中未定义"
                    })
        
        # 4. 检查一致性维度
        conformed_dim_names = {cd.get("dimension_name") for cd in model.conformed_dimensions}
        for cd_name in conformed_dim_names:
            if cd_name not in defined_dims:
                issues.append({
                    "severity": "error",
                    "dimension": cd_name,
                    "message": "一致性维度在维度列表中不存在"
                })
        
        # 更新验证结果
        model.validation_issues = issues
        if not any(i["severity"] == "error" for i in issues):
            model.status = "validated"
        db.commit()
        db.refresh(model)
        
        return {
            "model_id": model_id,
            "issues": issues,
            "status": model.status,
            "has_errors": any(i["severity"] == "error" for i in issues),
        }
    
    def confirm_model(self, db: Session, model_id: str) -> dict[str, Any]:
        """确认维度模型。"""
        model = db.query(DimensionalModel).filter(DimensionalModel.id == model_id).first()
        if not model:
            raise ValueError("维度模型不存在")
        
        if model.status not in ["validated", "draft"]:
            raise ValueError(f"模型状态 {model.status} 不允许确认")
        
        # 如果有错误级别的验证问题，不允许确认
        if model.validation_issues:
            has_errors = any(
                i.get("severity") == "error" 
                for i in model.validation_issues
            )
            if has_errors:
                raise ValueError("模型存在验证错误，请先修复")
        
        model.status = "confirmed"
        model.version += 1
        db.commit()
        db.refresh(model)
        
        # 如果关联了建模工单，推进工单状态
        if model.modeling_case_id:
            case = db.query(ModelingCase).filter(
                ModelingCase.id == model.modeling_case_id
            ).first()
            if case and case.stage in ["ontology_confirmed", "data_confirmed"]:
                case.stage = "model_confirmed"
                db.commit()
        
        return model.to_dict()
    
    def compile_model(self, db: Session, model_id: str) -> dict[str, Any]:
        """编译维度模型为物化契约。
        
        这是一个占位实现，实际应该：
        1. 为每个事实表生成一个物化契约
        2. 为每个维度生成一个物化契约
        3. 根据 SCD 类型生成相应的 ETL 逻辑
        4. 处理退化维度、角色扮演维度等特殊情况
        """
        model = db.query(DimensionalModel).filter(DimensionalModel.id == model_id).first()
        if not model:
            raise ValueError("维度模型不存在")
        
        if model.status != "confirmed":
            raise ValueError("只有已确认的模型才能编译")
        
        # TODO: 实际编译逻辑
        # 1. 创建维度表的物化契约
        # 2. 创建事实表的物化契约
        # 3. 生成 DDL、ETL、DAG
        
        compiled_contracts = []
        
        # 占位：记录需要生成的契约
        for dim in model.dimensions:
            compiled_contracts.append({
                "type": "dimension",
                "name": dim.get("name"),
                "status": "pending"
            })
        
        for fact in model.fact_tables:
            compiled_contracts.append({
                "type": "fact",
                "name": fact.get("name"),
                "status": "pending"
            })
        
        model.compiled_contracts = compiled_contracts
        model.compiled_at = datetime.now(timezone.utc)
        model.status = "compiled"
        db.commit()
        db.refresh(model)
        
        return {
            "model_id": model_id,
            "compiled_contracts": compiled_contracts,
            "message": "模型已编译（占位实现）"
        }
