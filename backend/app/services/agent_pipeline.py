"""治理制品流水线：草稿 → 校验 → 确认 → 执行 → 回执。

状态机（单向，非法跃迁一律拒绝）::

    drafted ──validate──> validated ──confirm──> confirmed ──execute──> succeeded
       ^                      │                                    └──> failed
       └── 校验有阻断项时停留 ─┘

关键不变量：
- **未确认不得执行**：``execute`` 只接受 ``confirmed`` 状态。
- **确认前必须有校验报告与 dry-run 差异**，人看得到将要发生什么。
- **幂等**：已 ``succeeded`` 的制品重复执行直接返回原回执，不产生第二次副作用。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.agents import registry
from app.agents.validation import is_blocking, validate_spec
from app.governance import active_standard
from app.models.agent import ArtifactKind, ArtifactStatus, GovernanceArtifact


class PipelineError(ValueError):
    """流水线状态或前置条件错误。"""


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _loads(raw: str | None, fallback):
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return fallback


class AgentPipelineService:
    # ---------- 草稿 ----------

    def draft(
        self,
        db: Session,
        *,
        kind: str,
        intent: str,
        context: dict[str, Any] | None = None,
        ontology_id: str | None = None,
    ) -> GovernanceArtifact:
        if kind not in {k.value for k in ArtifactKind}:
            raise PipelineError(
                f"未知制品类型 {kind}，可选：{', '.join(registry.all_kinds())}"
            )
        drafter = registry.get_drafter(kind)  # 未注册 → UnregisteredKindError
        spec = drafter.draft(intent, context or {})

        artifact = GovernanceArtifact(
            kind=kind,
            name=drafter.suggested_name(intent, spec),
            ontology_id=ontology_id,
            intent=intent,
            spec_json=_dumps(spec),
            machine_baseline=_dumps(spec),
            status=ArtifactStatus.DRAFTED.value,
        )
        db.add(artifact)
        db.commit()
        db.refresh(artifact)
        return artifact

    # ---------- 校验（含 dry-run） ----------

    def validate(
        self, db: Session, artifact_id: str, *, context: dict[str, Any] | None = None
    ) -> GovernanceArtifact:
        artifact = self._require(db, artifact_id)
        if artifact.status in (
            ArtifactStatus.EXECUTING.value,
            ArtifactStatus.SUCCEEDED.value,
        ):
            raise PipelineError(f"{artifact.status} 状态的制品不可重新校验")

        spec = _loads(artifact.spec_json, {})
        issues = validate_spec(
            db, kind=artifact.kind, spec=spec, ontology_id=artifact.ontology_id
        )
        blocking = [i for i in issues if is_blocking(i)]

        dry_run: dict[str, Any] | None = None
        dry_run_error: str | None = None
        if not blocking:
            try:
                dry_run = registry.get_executor(artifact.kind).dry_run(
                    spec, context or {}
                )
            except registry.UnregisteredKindError as exc:
                dry_run_error = str(exc)
            except Exception as exc:  # noqa: BLE001 —— dry-run 失败不应炸掉校验
                dry_run_error = f"dry-run 失败：{exc}"

        artifact.validation_report_json = _dumps(
            {
                "issues": [i.to_dict() for i in issues],
                "blocking_count": len(blocking),
                "dry_run": dry_run,
                "dry_run_error": dry_run_error,
                # 版本戳：审计「本制品在哪版规约下过闸」，规约升级后可据此判是否需 re-lint。
                "standard_version": active_standard(db).version,
                "validated_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        # 有阻断项、或高危制品拿不到 dry-run 差异 → 不得进入 validated。
        ready = not blocking and (
            dry_run is not None or not artifact.is_high_risk
        )
        artifact.status = (
            ArtifactStatus.VALIDATED.value if ready else ArtifactStatus.DRAFTED.value
        )
        db.commit()
        db.refresh(artifact)
        return artifact

    # ---------- 确认 ----------

    def confirm(
        self, db: Session, artifact_id: str, *, operator: str | None = None
    ) -> GovernanceArtifact:
        artifact = self._require(db, artifact_id)
        if artifact.status != ArtifactStatus.VALIDATED.value:
            raise PipelineError(
                f"只有 validated 状态可确认，当前为 {artifact.status}"
                + ("（请先执行校验）" if artifact.status == "drafted" else "")
            )
        report = _loads(artifact.validation_report_json, {})
        if artifact.is_high_risk and not report.get("dry_run"):
            raise PipelineError("高危制品必须先产出 dry-run 差异才可确认")

        artifact.status = ArtifactStatus.CONFIRMED.value
        artifact.confirmed_by = operator
        artifact.confirmed_at = datetime.now(timezone.utc)
        artifact.origin = "machine_edited"
        db.commit()
        db.refresh(artifact)
        return artifact

    # ---------- 执行 ----------

    def execute(
        self, db: Session, artifact_id: str, *, context: dict[str, Any] | None = None
    ) -> GovernanceArtifact:
        artifact = self._require(db, artifact_id)

        # 幂等：已成功的制品重复执行不产生第二次副作用。
        if artifact.status == ArtifactStatus.SUCCEEDED.value:
            return artifact
        if artifact.status != ArtifactStatus.CONFIRMED.value:
            raise PipelineError(
                f"只有 confirmed 状态可执行，当前为 {artifact.status}"
                "（未经人工确认的制品不得执行）"
            )

        executor = registry.get_executor(artifact.kind)
        spec = _loads(artifact.spec_json, {})
        artifact.status = ArtifactStatus.EXECUTING.value
        db.commit()

        try:
            receipt = executor.execute(spec, context or {})
        except Exception as exc:  # noqa: BLE001
            artifact.status = ArtifactStatus.FAILED.value
            artifact.execution_receipt_json = _dumps({"error": str(exc)})
            artifact.executed_at = datetime.now(timezone.utc)
            db.commit()
            db.refresh(artifact)
            return artifact

        artifact.status = ArtifactStatus.SUCCEEDED.value
        artifact.execution_receipt_json = _dumps(receipt)
        artifact.executed_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(artifact)
        return artifact

    # ---------- 查询 ----------

    def list_artifacts(
        self,
        db: Session,
        *,
        kind: str | None = None,
        status: str | None = None,
        ontology_id: str | None = None,
    ) -> list[GovernanceArtifact]:
        q = db.query(GovernanceArtifact)
        if kind:
            q = q.filter(GovernanceArtifact.kind == kind)
        if status:
            q = q.filter(GovernanceArtifact.status == status)
        if ontology_id:
            q = q.filter(GovernanceArtifact.ontology_id == ontology_id)
        return q.order_by(desc(GovernanceArtifact.created_at)).all()

    def get(self, db: Session, artifact_id: str) -> GovernanceArtifact | None:
        return db.get(GovernanceArtifact, artifact_id)

    def _require(self, db: Session, artifact_id: str) -> GovernanceArtifact:
        artifact = db.get(GovernanceArtifact, artifact_id)
        if artifact is None:
            raise LookupError("制品不存在")
        return artifact
