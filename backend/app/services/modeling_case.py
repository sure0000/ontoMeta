"""建模工单服务层。

核心职责：
- revision/hash 管理
- confirm/reject 事务
- stale 检测与失效
- rebase 操作
- 乐观锁并发控制
"""

import hashlib
import json
from datetime import datetime
from typing import Any

from sqlalchemy import and_, desc, or_
from sqlalchemy.orm import Session

from app.models.modeling import (
    ModelingCase,
    ModelingCaseLink,
    ModelingCaseSpec,
    ModelingCaseSpecKind,
    ModelingCaseSpecStatus,
    ModelingCaseStage,
)
from app.schemas.modeling import (
    ModelingCaseCreate,
    ModelingCaseSpecConfirm,
    ModelingCaseSpecReject,
    ModelingCaseSpecSave,
    ModelingCaseUpdate,
    validate_spec_payload,
)


class ModelingCaseService:
    """建模工单服务。"""
    
    # ========================================================================
    # 工单 CRUD
    # ========================================================================
    
    @staticmethod
    def create(db: Session, data: ModelingCaseCreate) -> ModelingCase:
        """创建建模工单。"""
        case = ModelingCase(
            title=data.title,
            conversation_id=data.conversation_id,
            primary_domain_id=data.primary_domain_id,
            domain_ids_json=json.dumps(data.domain_ids) if data.domain_ids else None,
            owner_subject_id=data.owner_subject_id,
            stage=ModelingCaseStage.COLLECTING_REQUIREMENT.value,
            current_revision=0,
        )
        db.add(case)
        db.commit()
        db.refresh(case)
        return case
    
    @staticmethod
    def get(db: Session, case_id: str) -> ModelingCase | None:
        """获取工单。"""
        return db.query(ModelingCase).filter(ModelingCase.id == case_id).first()
    
    @staticmethod
    def list_cases(
        db: Session,
        *,
        stage: str | None = None,
        owner_subject_id: str | None = None,
        conversation_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ModelingCase]:
        """列表与筛选。"""
        query = db.query(ModelingCase)
        
        if stage:
            query = query.filter(ModelingCase.stage == stage)
        if owner_subject_id:
            query = query.filter(ModelingCase.owner_subject_id == owner_subject_id)
        if conversation_id:
            query = query.filter(ModelingCase.conversation_id == conversation_id)
        
        return (
            query.order_by(desc(ModelingCase.updated_at))
            .limit(limit)
            .offset(offset)
            .all()
        )
    
    @staticmethod
    def update(db: Session, case_id: str, data: ModelingCaseUpdate) -> ModelingCase:
        """更新非确认字段。"""
        case = ModelingCaseService.get(db, case_id)
        if case is None:
            raise ValueError(f"工单 {case_id} 不存在")
        
        if data.title is not None:
            case.title = data.title
        if data.owner_subject_id is not None:
            case.owner_subject_id = data.owner_subject_id
        if data.blocked_reason is not None:
            case.blocked_reason = data.blocked_reason
        
        db.commit()
        db.refresh(case)
        return case
    
    # ========================================================================
    # 规格 revision 与 hash
    # ========================================================================
    
    @staticmethod
    def _compute_hash(payload: dict[str, Any]) -> str:
        """计算规格内容哈希（用于检测变化与幂等）。"""
        canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canonical.encode()).hexdigest()
    
    @staticmethod
    def save_spec(
        db: Session,
        case_id: str,
        kind: str,
        data: ModelingCaseSpecSave,
    ) -> ModelingCaseSpec:
        """保存新 draft revision。
        
        相同 content_hash 不创建新 revision（幂等）。
        """
        case = ModelingCaseService.get(db, case_id)
        if case is None:
            raise ValueError(f"工单 {case_id} 不存在")
        
        # 强类型校验
        validate_spec_payload(kind, data.payload)
        
        content_hash = ModelingCaseService._compute_hash(data.payload)
        
        # 检查是否已有相同 hash 的 draft
        existing = (
            db.query(ModelingCaseSpec)
            .filter(
                and_(
                    ModelingCaseSpec.case_id == case_id,
                    ModelingCaseSpec.kind == kind,
                    ModelingCaseSpec.content_hash == content_hash,
                    ModelingCaseSpec.status == ModelingCaseSpecStatus.DRAFT.value,
                )
            )
            .first()
        )
        
        if existing:
            return existing
        
        # 计算新 revision
        max_rev = (
            db.query(ModelingCaseSpec.revision)
            .filter(
                and_(
                    ModelingCaseSpec.case_id == case_id,
                    ModelingCaseSpec.kind == kind,
                )
            )
            .order_by(desc(ModelingCaseSpec.revision))
            .first()
        )
        new_revision = (max_rev[0] + 1) if max_rev else 1
        
        # 获取上游依赖
        based_on = ModelingCaseService._collect_based_on(db, case_id, kind)
        
        spec = ModelingCaseSpec(
            case_id=case_id,
            kind=kind,
            revision=new_revision,
            status=ModelingCaseSpecStatus.DRAFT.value,
            payload_json=json.dumps(data.payload, ensure_ascii=False),
            content_hash=content_hash,
            based_on_json=json.dumps(based_on) if based_on else None,
            proposed_by=data.proposed_by,
        )
        
        db.add(spec)
        db.commit()
        db.refresh(spec)
        return spec
    
    @staticmethod
    def _collect_based_on(db: Session, case_id: str, kind: str) -> list[dict[str, Any]]:
        """收集上游确认版本（用于失效判断）。"""
        upstream_kinds = ModelingCaseService._get_upstream_kinds(kind)
        based_on: list[dict[str, Any]] = []
        
        for upstream_kind in upstream_kinds:
            confirmed = ModelingCaseService.get_confirmed_spec(db, case_id, upstream_kind)
            if confirmed:
                based_on.append({
                    "kind": confirmed.kind,
                    "revision": confirmed.revision,
                    "hash": confirmed.content_hash,
                })
        
        return based_on
    
    @staticmethod
    def _get_upstream_kinds(kind: str) -> list[str]:
        """获取依赖的上游 kind。"""
        dependencies = {
            ModelingCaseSpecKind.CONTEXT.value: [ModelingCaseSpecKind.REQUIREMENT.value],
            ModelingCaseSpecKind.DIMENSIONAL_MODEL.value: [
                ModelingCaseSpecKind.REQUIREMENT.value,
                ModelingCaseSpecKind.CONTEXT.value,
            ],
            ModelingCaseSpecKind.LOGIC_BUNDLE.value: [
                ModelingCaseSpecKind.DIMENSIONAL_MODEL.value,
            ],
            ModelingCaseSpecKind.DELIVERY.value: [
                ModelingCaseSpecKind.LOGIC_BUNDLE.value,
            ],
            ModelingCaseSpecKind.ACCEPTANCE.value: [
                ModelingCaseSpecKind.DELIVERY.value,
            ],
        }
        return dependencies.get(kind, [])
    
    # ========================================================================
    # 确认与拒绝
    # ========================================================================
    
    @staticmethod
    def confirm_spec(
        db: Session,
        case_id: str,
        kind: str,
        revision: int,
        data: ModelingCaseSpecConfirm,
    ) -> ModelingCaseSpec:
        """确认规格并推进阶段（乐观锁）。"""
        spec = (
            db.query(ModelingCaseSpec)
            .filter(
                and_(
                    ModelingCaseSpec.case_id == case_id,
                    ModelingCaseSpec.kind == kind,
                    ModelingCaseSpec.revision == revision,
                )
            )
            .first()
        )
        
        if spec is None:
            raise ValueError(f"规格 {kind} revision {revision} 不存在")
        
        # 乐观锁
        if spec.content_hash != data.content_hash:
            raise ValueError(
                f"规格已变化（期望 hash {data.content_hash[:8]}，实际 {spec.content_hash[:8]}）"
            )
        
        if spec.status != ModelingCaseSpecStatus.DRAFT.value:
            raise ValueError(f"只能确认 draft 状态的规格，当前状态：{spec.status}")
        
        # 标记旧的 confirmed 为 superseded
        old_confirmed = ModelingCaseService.get_confirmed_spec(db, case_id, kind)
        if old_confirmed:
            old_confirmed.status = ModelingCaseSpecStatus.SUPERSEDED.value
        
        # 确认当前版本
        spec.status = ModelingCaseSpecStatus.CONFIRMED.value
        spec.confirmed_by = data.confirmed_by
        spec.confirmed_at = datetime.utcnow()
        
        # 推进工单阶段
        case = ModelingCaseService.get(db, case_id)
        if case:
            new_stage = ModelingCaseService._next_stage_for_kind(kind)
            if new_stage and case.stage != new_stage:
                case.stage = new_stage
                case.current_revision += 1
        
        db.commit()
        db.refresh(spec)
        return spec
    
    @staticmethod
    def _next_stage_for_kind(kind: str) -> str | None:
        """确认某类规格后应进入的阶段。"""
        stage_map = {
            ModelingCaseSpecKind.REQUIREMENT.value: ModelingCaseStage.REQUIREMENT_CONFIRMED.value,
            ModelingCaseSpecKind.CONTEXT.value: ModelingCaseStage.CONTEXT_CONFIRMED.value,
            ModelingCaseSpecKind.DIMENSIONAL_MODEL.value: ModelingCaseStage.MODEL_CONFIRMED.value,
            ModelingCaseSpecKind.LOGIC_BUNDLE.value: ModelingCaseStage.PLAN_CONFIRMED.value,
            ModelingCaseSpecKind.DELIVERY.value: ModelingCaseStage.PLAN_CONFIRMED.value,
        }
        return stage_map.get(kind)
    
    @staticmethod
    def reject_spec(
        db: Session,
        case_id: str,
        kind: str,
        revision: int,
        data: ModelingCaseSpecReject,
    ) -> ModelingCaseSpec:
        """拒绝规格并记录原因。"""
        spec = (
            db.query(ModelingCaseSpec)
            .filter(
                and_(
                    ModelingCaseSpec.case_id == case_id,
                    ModelingCaseSpec.kind == kind,
                    ModelingCaseSpec.revision == revision,
                )
            )
            .first()
        )
        
        if spec is None:
            raise ValueError(f"规格 {kind} revision {revision} 不存在")
        
        spec.status = ModelingCaseSpecStatus.REJECTED.value
        spec.validation_report_json = json.dumps(
            {"rejected": True, "reason": data.reason, "rejected_by": data.rejected_by},
            ensure_ascii=False,
        )
        
        db.commit()
        db.refresh(spec)
        return spec
    
    # ========================================================================
    # Stale 检测与 rebase
    # ========================================================================
    
    @staticmethod
    def check_stale(db: Session, case_id: str, kind: str) -> dict[str, Any]:
        """检查规格是否因上游变化而 stale。"""
        confirmed = ModelingCaseService.get_confirmed_spec(db, case_id, kind)
        if not confirmed:
            return {"is_stale": False, "reason": "无已确认版本"}
        
        if not confirmed.based_on_json:
            return {"is_stale": False, "reason": "无上游依赖"}
        
        based_on: list[dict[str, Any]] = json.loads(confirmed.based_on_json)
        stale_upstreams: list[dict[str, Any]] = []
        
        for upstream in based_on:
            current_confirmed = ModelingCaseService.get_confirmed_spec(
                db, case_id, upstream["kind"]
            )
            if not current_confirmed:
                stale_upstreams.append({
                    "kind": upstream["kind"],
                    "reason": "上游版本已删除",
                })
            elif current_confirmed.content_hash != upstream["hash"]:
                stale_upstreams.append({
                    "kind": upstream["kind"],
                    "old_revision": upstream["revision"],
                    "new_revision": current_confirmed.revision,
                    "reason": "上游版本已变化",
                })
        
        if stale_upstreams:
            # 标记为 stale
            confirmed.status = ModelingCaseSpecStatus.STALE.value
            db.commit()
            return {
                "is_stale": True,
                "stale_upstreams": stale_upstreams,
            }
        
        return {"is_stale": False}
    
    @staticmethod
    def rebase(db: Session, case_id: str, kind: str) -> ModelingCaseSpec:
        """显式 rebase：基于新上游重新生成 draft。
        
        当前实现：清除 stale 标记，要求用户重新提交 draft。
        未来可扩展为自动合并或 LLM 辅助 rebase。
        """
        stale_spec = (
            db.query(ModelingCaseSpec)
            .filter(
                and_(
                    ModelingCaseSpec.case_id == case_id,
                    ModelingCaseSpec.kind == kind,
                    ModelingCaseSpec.status == ModelingCaseSpecStatus.STALE.value,
                )
            )
            .first()
        )
        
        if not stale_spec:
            raise ValueError(f"规格 {kind} 不是 stale 状态，无需 rebase")
        
        # 简化第一版：清除 stale，用户需重新提交新 draft
        # P3-P4 可扩展为自动基于新上游重新生成
        stale_spec.status = ModelingCaseSpecStatus.SUPERSEDED.value
        db.commit()
        
        return stale_spec
    
    # ========================================================================
    # 辅助查询
    # ========================================================================
    
    @staticmethod
    def get_confirmed_spec(db: Session, case_id: str, kind: str) -> ModelingCaseSpec | None:
        """获取当前确认版本。"""
        return (
            db.query(ModelingCaseSpec)
            .filter(
                and_(
                    ModelingCaseSpec.case_id == case_id,
                    ModelingCaseSpec.kind == kind,
                    ModelingCaseSpec.status == ModelingCaseSpecStatus.CONFIRMED.value,
                )
            )
            .first()
        )
    
    @staticmethod
    def get_latest_draft(db: Session, case_id: str, kind: str) -> ModelingCaseSpec | None:
        """获取最新 draft。"""
        return (
            db.query(ModelingCaseSpec)
            .filter(
                and_(
                    ModelingCaseSpec.case_id == case_id,
                    ModelingCaseSpec.kind == kind,
                    ModelingCaseSpec.status == ModelingCaseSpecStatus.DRAFT.value,
                )
            )
            .order_by(desc(ModelingCaseSpec.revision))
            .first()
        )
    
    @staticmethod
    def list_specs(
        db: Session,
        case_id: str,
        kind: str | None = None,
        status: str | None = None,
    ) -> list[ModelingCaseSpec]:
        """列出工单的所有规格。"""
        query = db.query(ModelingCaseSpec).filter(ModelingCaseSpec.case_id == case_id)
        
        if kind:
            query = query.filter(ModelingCaseSpec.kind == kind)
        if status:
            query = query.filter(ModelingCaseSpec.status == status)
        
        return query.order_by(
            ModelingCaseSpec.kind, desc(ModelingCaseSpec.revision)
        ).all()
    
    # ========================================================================
    # 引用管理
    # ========================================================================
    
    @staticmethod
    def add_link(
        db: Session,
        case_id: str,
        ref_kind: str,
        ref_id: str,
        role: str,
        spec_revision: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ModelingCaseLink:
        """添加外部实体引用。"""
        # 检查是否已存在
        existing = (
            db.query(ModelingCaseLink)
            .filter(
                and_(
                    ModelingCaseLink.case_id == case_id,
                    ModelingCaseLink.ref_kind == ref_kind,
                    ModelingCaseLink.ref_id == ref_id,
                    ModelingCaseLink.role == role,
                )
            )
            .first()
        )
        
        if existing:
            return existing
        
        link = ModelingCaseLink(
            case_id=case_id,
            ref_kind=ref_kind,
            ref_id=ref_id,
            role=role,
            spec_revision=spec_revision,
            metadata_json=json.dumps(metadata) if metadata else None,
        )
        
        db.add(link)
        db.commit()
        db.refresh(link)
        return link
    
    @staticmethod
    def list_links(
        db: Session,
        case_id: str,
        ref_kind: str | None = None,
        role: str | None = None,
    ) -> list[ModelingCaseLink]:
        """列出工单的引用。"""
        query = db.query(ModelingCaseLink).filter(ModelingCaseLink.case_id == case_id)
        
        if ref_kind:
            query = query.filter(ModelingCaseLink.ref_kind == ref_kind)
        if role:
            query = query.filter(ModelingCaseLink.role == role)
        
        return query.all()


__all__ = ["ModelingCaseService"]
