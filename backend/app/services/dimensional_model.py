"""维度模型服务：设计、验证、编译维度模型。"""
import json
import uuid
from datetime import UTC, datetime
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
        
        model.updated_at = datetime.now(UTC)
        db.commit()
        db.refresh(model)
        return model.to_dict()
    
    def validate_model(self, db: Session, model_id: str) -> dict[str, Any]:
        """校验维度模型：粒度、度量可加性、SCD 配置、维度引用、一致性维度。

        规则本身住在 ``dimensional_model_validator``——那是按 Kimball 范式写的一整套，
        此前因为它读的是 ``DimensionalModelSpecV2`` 而落库的是另一套字段名，一条也跑不了，
        整个模块成了没有调用方的死代码，而这里手写了一份薄得多的重复校验。
        现在两边经 ``dimensional_model_spec.to_spec`` 对上，校验只有一处实现。
        """
        from pydantic import ValidationError

        from app.services.dimensional_model_spec import to_spec
        from app.services.dimensional_model_validator import validate_dimensional_model

        model = db.query(DimensionalModel).filter(DimensionalModel.id == model_id).first()
        if not model:
            raise ValueError("维度模型不存在")

        try:
            spec = to_spec(model)
        except ValidationError as exc:
            # 存的 JSON 连规格都组不成（缺粒度、缺名称这类必填项）。这**就是**一种校验失败，
            # 该报成 issue 让人在界面上看到哪一格没填——而不是让 500 从端点漏出去，
            # 那样用户只会看到「服务端内部错误」，完全不知道要改什么。
            issues = [
                {
                    "severity": "error",
                    "category": "schema",
                    "message": f"{'.'.join(str(p) for p in err['loc'])}：{err['msg']}",
                    "context": {"field": ".".join(str(p) for p in err["loc"])},
                }
                for err in exc.errors()
            ]
            model.validation_issues = issues
            model.status = "draft"
            db.commit()
            db.refresh(model)
            return {
                "model_id": model_id,
                "status": model.status,
                "issues": issues,
                "error_count": len(issues),
                "warning_count": 0,
            }

        issues = validate_dimensional_model(spec)

        model.validation_issues = issues
        has_error = any(i.get("severity") == "error" for i in issues)
        # 有 error 就退回 draft：让「已校验」这个状态始终意味着「可以确认了」。
        model.status = "draft" if has_error else "validated"
        db.commit()
        db.refresh(model)

        return {
            "model_id": model_id,
            "status": model.status,
            "issues": issues,
            "error_count": sum(1 for i in issues if i.get("severity") == "error"),
            "warning_count": sum(1 for i in issues if i.get("severity") == "warning"),
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
        """把维度模型编译成物化契约。

        产出的是 ``MaterializationContract``——本仓已有的、被物化流水线真正消费的那种制品，
        不另立一套。编译只写控制面（每个维度/事实在哪一层、SCD 怎么处理、怎么装载），
        物理 DDL/ETL 仍由既有的 warehouse_generator 按契约生成。

        落层与装载的判据::

            维度 scd_type=type2  → dim 层，scd_type=scd2，增量装载
                                   （全量重载会把历史版本冲掉，那正是 SCD2 要留的东西）
            维度 scd_type=type1  → dim 层，scd_type=scd1，全量装载（就地覆盖，无历史）
            维度 scd_type=none   → dim 层，全量装载
            事实表               → dwd 层，声明了 partition_by 则增量 + 该列为分区键，否则全量

        四类特殊情况（``compiled_contracts`` 里各自留痕，不静默跳过）：

        - **退化维度**：``order_id`` 这类维度只是事实表上的一列，没有独立维表，
          因此不生成契约——但会在回执里列出来，否则「说好的维度怎么没编译出来」无从对账。
        - **角色扮演维度**：``order_date`` / ``ship_date`` 指向同一张 ``dim_date``，
          物理上只有一张表。只为基础维度生成契约，角色本身记为别名。
        - **日期维度等生成维**：``ontology_object_ref`` 为空，没有本体对象可挂契约
          （契约的主键是「本体实体」）。报为 unsupported，等日期维生成器就位再接。
        - **SCD type3**：契约的 ``scd_type`` 只表达得了 none/scd1/scd2。type3 是列内多版本，
          没有对应落层语义——报出来，绝不悄悄降级成 scd1 让人以为历史留住了。

        **人工钉住的字段不覆盖**：与 ``MaterializationContractService.sync`` 同一套三方合并
        语义（``pinned_fields`` / ``machine_baseline``）。编译是机器行为，不该冲掉人在
        契约页上按过的决定。
        """
        from pydantic import ValidationError

        from app.models.warehouse import MaterializationContract, TargetKind
        from app.services.dimensional_model_spec import SPEC_SCD_TO_CONTRACT, to_spec

        model = db.query(DimensionalModel).filter(DimensionalModel.id == model_id).first()
        if not model:
            raise ValueError("维度模型不存在")

        if model.status != "confirmed":
            raise ValueError("只有已确认的模型才能编译")

        try:
            spec = to_spec(model)
        except ValidationError as exc:
            # 与 validate_model 同样的理由：组不成规格是数据问题，该说清哪一格不对。
            raise ValueError(
                "模型定义不完整，无法编译："
                + "；".join(
                    f"{'.'.join(str(p) for p in err['loc'])} {err['msg']}"
                    for err in exc.errors()[:5]
                )
            ) from exc

        # 角色扮演维度：角色名 → 基础维度名。物理上共用一张表，只编译基础维度。
        role_to_base = {
            rp.role: rp.base_dimension
            for rp in spec.role_playing_dimensions
            if rp.role and rp.base_dimension
        }

        existing = {
            (c.target_kind, c.target_id): c
            for c in db.query(MaterializationContract)
            .filter(MaterializationContract.ontology_id == model.ontology_id)
            .all()
        }

        compiled: list[dict[str, Any]] = []
        unsupported: list[dict[str, Any]] = []
        seen_targets: dict[str, str] = {}  # object_id → 已占用它的维度/事实名

        def _emit(
            *,
            kind: str,
            name: str,
            object_ref: str | None,
            layer: str,
            scd: str,
            strategy: str,
            partition_key: str | None,
            reason: str,
        ) -> None:
            """把一条编译结果落成契约（或记为不可编译）。"""
            if not object_ref:
                unsupported.append({
                    "type": kind,
                    "name": name,
                    "reason": "未绑定本体对象（生成维/派生表尚不支持编译成契约）",
                })
                return
            if object_ref in seen_targets:
                # 契约按「本体实体」唯一。两个维度指向同一个对象时，后者会覆盖前者——
                # 那是设计里的真冲突（同一张表两套 SCD 策略），必须报出来让人裁决。
                unsupported.append({
                    "type": kind,
                    "name": name,
                    "reason": f"本体对象已被「{seen_targets[object_ref]}」占用，同一对象不能编译出两份契约",
                })
                return
            seen_targets[object_ref] = name

            fields = {
                "target_layer": layer,
                "target_engines": json.dumps(["doris"], ensure_ascii=False),
                "load_strategy": strategy,
                "partition_key": partition_key,
                "scd_type": scd,
                "materialized": True,
            }
            baseline = json.dumps(fields, ensure_ascii=False)
            key = (TargetKind.OBJECT_TYPE.value, object_ref)
            contract = existing.get(key)
            pinned_skipped: list[str] = []

            if contract is None:
                contract = MaterializationContract(
                    ontology_id=model.ontology_id,
                    target_kind=TargetKind.OBJECT_TYPE.value,
                    target_id=object_ref,
                    machine_baseline=baseline,
                    derivation_reason=reason,
                    **fields,
                )
                db.add(contract)
                db.flush()  # 取 id 回填进 compiled_contracts
                existing[key] = contract
                action = "created"
            else:
                pinned = set(contract.pinned_fields)
                for field, value in fields.items():
                    if field in pinned:
                        pinned_skipped.append(field)
                        continue
                    setattr(contract, field, value)
                contract.machine_baseline = baseline
                contract.derivation_reason = reason
                contract.upstream_removed = False
                action = "updated"

            compiled.append({
                "type": kind,
                "name": name,
                "contract_id": contract.id,
                "object_type_id": object_ref,
                "target_layer": layer,
                "scd_type": scd,
                "load_strategy": strategy,
                "partition_key": partition_key,
                "action": action,
                "pinned_fields_kept": sorted(pinned_skipped),
                "status": "compiled",
            })

        # ---- 维度 ----
        for dim in spec.dimensions:
            if dim.name in role_to_base:
                compiled.append({
                    "type": "dimension",
                    "name": dim.name,
                    "status": "alias",
                    "aliases": role_to_base[dim.name],
                    "note": "角色扮演维度与基础维度共用一张物理表，不单独编译",
                })
                continue
            if dim.scd_type == "type3":
                unsupported.append({
                    "type": "dimension",
                    "name": dim.name,
                    "reason": "物化契约的 scd_type 只支持 none/scd1/scd2；type3（列内多版本）无对应落层语义",
                })
                continue
            contract_scd = SPEC_SCD_TO_CONTRACT.get(dim.scd_type, "none")
            # SCD2 必须增量：全量重载会把历史版本冲掉，而留住历史正是 SCD2 的全部意义。
            strategy = "incremental" if contract_scd == "scd2" else "full"
            _emit(
                kind="dimension",
                name=dim.name,
                object_ref=dim.ontology_object_ref,
                layer="dim",
                scd=contract_scd,
                strategy=strategy,
                partition_key=dim.effective_date_column if contract_scd == "scd2" else None,
                reason=f"维度模型「{model.name}」→ {dim.name}（SCD {dim.scd_type}）",
            )

        # ---- 事实表 ----
        for fact in spec.facts:
            partition_key = fact.partition_by
            _emit(
                kind="fact",
                name=fact.name,
                object_ref=fact.ontology_object_ref,
                layer="dwd",
                scd="none",  # 事实表不做缓慢变化，历史由粒度本身承载
                strategy="incremental" if partition_key else "full",
                partition_key=partition_key,
                reason=f"维度模型「{model.name}」→ 事实表 {fact.name}（粒度：{fact.grain or '未声明'}）",
            )
            # 退化维度只是事实表上的列，没有独立维表——留痕，不生成契约。
            for degenerate in fact.degenerate_dimensions:
                compiled.append({
                    "type": "degenerate_dimension",
                    "name": degenerate,
                    "fact": fact.name,
                    "status": "inline",
                    "note": "退化维度作为事实表上的一列存在，无独立维表",
                })

        model.compiled_contracts = compiled + (
            [{"status": "unsupported", **u} for u in unsupported]
        )
        model.compiled_at = datetime.now(UTC)
        model.status = "compiled"
        db.commit()
        db.refresh(model)

        contract_count = sum(1 for c in compiled if c.get("contract_id"))
        return {
            "model_id": model_id,
            "compiled_contracts": model.compiled_contracts,
            "contracts_written": contract_count,
            "unsupported": unsupported,
            "message": (
                f"已编译 {contract_count} 份物化契约"
                + (f"，{len(unsupported)} 项无法编译（见 unsupported）" if unsupported else "")
            ),
        }
