"""治理智能体流水线（M5 · 写侧）。

通用 Agent 的治理流水线：通过 MCP 暴露并共用本体做 grounding。

权限：本路由整体归 publisher —— 写侧智能体会改集群、建表、执行 SQL，
不能让 editor 触碰（策略见 ``auth._ROLE_OVERRIDES``）。
"""

import json
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.agents import registry
from app.api.deps import agent_pipeline
from app.auth import principal_label
from app.database import get_db
from app.models.agent import HIGH_RISK_KINDS, GovernanceArtifact
from app.schemas import (
    AgentKindsOut,
    ArtifactAgentApprovalRequest,
    ArtifactConfirmRequest,
    ArtifactDraftRequest,
    ArtifactEditRequest,
    ArtifactExecuteRequest,
    ArtifactResultRequest,
    GovernanceArtifactOut,
    TaskFormRequest,
)
from app.schemas.task_form import TaskFormResponse
from app.services.agent_pipeline import PipelineError
from app.services.task_form import ACTION_KIND_LABEL, build_task_form

router = APIRouter()


def _loads(raw: str | None):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None


def _to_out(
    a: GovernanceArtifact,
    *,
    live_state: dict | None = None,
    include_details: bool = True,
) -> GovernanceArtifactOut:
    """制品 → 输出 DTO。

    ``live_state`` 是从 Airflow 实时回读的 DagRun 状态。当它存在且非终态时
    (running/queued)，制品的 ``status`` 虽已置 succeeded（表示 DAG 提交成功），
    但实际还在跑——此时输出 status 覆写为 ``executing``，使前端展示与 Airflow 一致。
    """
    effective_status = a.status
    if live_state and not live_state.get("terminal", True):
        effective_status = "executing"
    return GovernanceArtifactOut(
        id=a.id,
        kind=a.kind,
        name=a.name,
        ontology_id=a.ontology_id,
        intent=a.intent,
        spec=(_loads(a.spec_json) or {}) if include_details else {},
        status=effective_status,
        is_high_risk=a.is_high_risk,
        validation_report=(
            _loads(a.validation_report_json) if include_details else None
        ),
        execution_receipt=(
            _loads(a.execution_receipt_json) if include_details else None
        ),
        confirmed_by=a.confirmed_by,
        confirmed_at=a.confirmed_at,
        executed_at=a.executed_at,
        agent_execution_approved=bool(a.agent_execution_approved),
        agent_execution_approved_by=a.agent_execution_approved_by,
        agent_execution_approved_at=a.agent_execution_approved_at,
        origin=a.origin,
        result_outcome=a.result_outcome,
        result_note=a.result_note,
        result_confirmed_by=a.result_confirmed_by,
        result_confirmed_at=a.result_confirmed_at,
        result_via=a.result_via,
        pinned_fields=a.pinned_fields,
        created_by=a.created_by,
        created_via=a.created_via,
        created_at=a.created_at,
        updated_at=a.updated_at,
    )


def _guard(fn):
    """统一把流水线错误翻成 HTTP 状态。"""
    try:
        return fn()
    # UnregisteredKindError 继承自 LookupError，必须排在前面，否则会被 404 吞掉。
    except registry.UnregisteredKindError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    # PipelineError 继承自 ValueError，必须排在其前面。
    except PipelineError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    # Drafter/Executor 对无效意图或缺失上下文抛 ValueError —— 属输入问题，非服务端故障。
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/agents/kinds", response_model=AgentKindsOut)
def list_agent_kinds():
    """制品类型与实现进度。``registered`` 之外的类型调用会返回 501。"""
    return AgentKindsOut(
        all_kinds=registry.all_kinds(),
        registered=registry.registered_kinds(),
        high_risk=sorted(HIGH_RISK_KINDS),
    )


@router.get("/agents/artifacts", response_model=list[GovernanceArtifactOut])
def list_artifacts(
    kind: str | None = Query(None),
    status: str | None = Query(None),
    ontology_id: str | None = Query(None),
    reconcile: bool = Query(False),
    include_details: bool = Query(False),
    db: Session = Depends(get_db),
):
    """返回任务目录；实时对账和大字段只在显式请求时启用。"""
    rows = agent_pipeline.list_artifacts(
        db,
        kind=kind,
        status=status,
        ontology_id=ontology_id,
        reconcile=reconcile,
    )
    # P0a：列表端点也回读 live_state（只对 materialize，其他类型不需要轮询 Airflow）
    out = []
    for a in rows:
        ls = _try_live_state(db, a) if reconcile and a.kind == "materialize" else None
        item = _to_out(a, live_state=ls, include_details=include_details)
        item.live_state = ls
        out.append(item)
    return out


@router.get("/agents/artifacts/{artifact_id}", response_model=GovernanceArtifactOut)
def get_artifact(
    artifact_id: str,
    reconcile: bool = Query(True),
    db: Session = Depends(get_db),
):
    artifact = agent_pipeline.get(db, artifact_id, reconcile=reconcile)
    if artifact is None:
        raise HTTPException(status_code=404, detail="制品不存在")
    # P1-6：best-effort 回读 DagRun 实时态（失败退制品 status）。
    ls = _try_live_state(db, artifact) if reconcile else None
    out = _to_out(artifact, live_state=ls)
    out.live_state = ls
    return out


def _try_live_state(db: Session, artifact: GovernanceArtifact) -> dict | None:
    """尽力回读一个制品的 Airflow 实时态（多批 DagRun 聚合）。从不抛异常。

    制品提交 DAG 后保持 executing；Airflow 终态是编排事实，sync 还要继续验证 Doris
    目标表。复用 warehouse 的批次解析 + 状态聚合，读不到就返回 None。

    **状态回写**：当 Airflow DagRun 已达终态时，把制品 status 同步到与 Airflow 一致
    (success→succeeded, failed→failed)。这样即使后续 Airflow 不可达，制品状态也是对的。
    """
    try:
        from app.api.warehouse import _aggregate_state, _receipt_batches
        from app.connectors.airflow import AirflowClient, AirflowError, is_terminal
        from app.services.settings_service import SettingsService

        batches = _receipt_batches(db, artifact.id)
        if not batches:
            return None
        rt = SettingsService().get_airflow_runtime(db)
        if not rt.available:
            return None
        client = AirflowClient(
            rt.endpoint, username=rt.username, password=rt.password,
        )
        try:
            states: list = []
            run_url = None
            for b in batches:
                bid, brun = b.get("dag_id"), b.get("dag_run_id")
                if not bid or not brun:
                    states.append(b.get("state") or "failed")
                    continue
                try:
                    run = client.get_dag_run(bid, brun)
                    states.append(run.get("state"))
                    run_url = run_url or client.run_url(bid, brun)
                except AirflowError:
                    states.append(None)
        finally:
            client.close()
        agg = _aggregate_state(states)
        if not agg:
            return None
        terminal = is_terminal(agg)
        verification = None
        # 终态回写：同步任务还须通过 Doris 结果验证，不能只认 Airflow success。
        if terminal and artifact.status in {"executing", "succeeded"}:
            from app.models.agent import ArtifactStatus

            if artifact.kind == "sync" and agg == "success":
                from app.services.sync_reconciliation import reconcile_sync_receipt

                try:
                    receipt = json.loads(artifact.execution_receipt_json or "{}")
                except (TypeError, ValueError):
                    receipt = {}
                verification = reconcile_sync_receipt(
                    db, receipt=receipt, airflow_state=agg
                )
                if verification is not None:
                    receipt["doris_verification"] = verification
                    artifact.execution_receipt_json = json.dumps(
                        receipt, ensure_ascii=False, default=str
                    )
            verified = not verification or verification.get("verified") is True
            new_status = (
                ArtifactStatus.SUCCEEDED.value
                if agg == "success" and verified
                else ArtifactStatus.FAILED.value
            )
            if new_status != artifact.status:
                artifact.status = new_status
            db.commit()
        elif not terminal and artifact.status != "executing":
            artifact.status = "executing"
            db.commit()
        display_state = "failed" if verification and not verification.get("verified") else agg
        return {
            "live_state": display_state,
            "airflow_state": agg,
            "terminal": terminal,
            "run_url": run_url,
        }
    except Exception:  # noqa: BLE001 — 实时态是增强，读不到退回制品态，绝不炸 API
        return None


@router.post("/agents/draft", response_model=GovernanceArtifactOut)
def draft_artifact(
    data: ArtifactDraftRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """自然语言意图 → 声明式 Spec。LLM 只产规格，不产命令。"""
    artifact = _guard(
        lambda: agent_pipeline.draft(
            db,
            kind=data.kind,
            intent=data.intent,
            context=data.context,
            ontology_id=data.ontology_id,
            spec=data.spec,
            name=data.name,
            user_created=data.user_created,
            created_by=principal_label(db, request),
            created_via="frontend",
        )
    )
    return _to_out(artifact)


@router.post("/agents/task-form", response_model=TaskFormResponse)
def task_confirmation_form(data: TaskFormRequest, db: Session = Depends(get_db)):
    """按任务类型现取一张**建数表单**（字段骨架 + 真实候选 + 本次 confirmation_id）。

    MCP flow 使用的也是同一张表；这个端点给**非模型触发**的入口用
    ——任务链要逐步确认时，第 N 步也得拿到和单发任务一模一样的向导，否则「链上的任务
    可以少确认几环」就成了事实上的旁路。
    """
    from app.models import Ontology

    if data.kind not in registry.registered_kinds():
        raise HTTPException(status_code=400, detail=f"未知任务类型：{data.kind}")
    if db.get(Ontology, data.ontology_id) is None:
        raise HTTPException(status_code=404, detail="本体不存在")
    form = _guard(
        lambda: build_task_form(
            db,
            kind=data.kind,
            ontology_id=data.ontology_id,
            title=data.title or f"确认{ACTION_KIND_LABEL.get(data.kind, data.kind)}任务",
            intent=data.intent,
            prefill=data.prefill,
        )
    )
    return TaskFormResponse(**{k: v for k, v in form.items() if k != "prefilled"})


@router.patch("/agents/artifacts/{artifact_id}", response_model=GovernanceArtifactOut)
def edit_artifact(
    artifact_id: str, data: ArtifactEditRequest, db: Session = Depends(get_db)
):
    """编辑草稿/已校验/失败态的制品。给 spec 直填、给 intent/context 走 drafter 重派生；
    编辑后 status 打回 drafted，旧校验/确认记录一并清空。已确认/执行过的制品拒改（409）。"""
    artifact = _guard(
        lambda: agent_pipeline.edit(
            db,
            artifact_id,
            name=data.name,
            intent=data.intent,
            context=data.context,
            spec=data.spec,
            ontology_id=data.ontology_id,
        )
    )
    return _to_out(artifact)


@router.post(
    "/agents/artifacts/{artifact_id}/validate", response_model=GovernanceArtifactOut
)
def validate_artifact(
    artifact_id: str, data: ArtifactExecuteRequest | None = None, db: Session = Depends(get_db)
):
    """过 Validation Gate 并产出 dry-run 差异。有阻断项则停留在 drafted。"""
    context = data.context if data else {}
    artifact = _guard(
        lambda: agent_pipeline.validate(db, artifact_id, context=context)
    )
    return _to_out(artifact)


@router.post(
    "/agents/artifacts/{artifact_id}/confirm", response_model=GovernanceArtifactOut
)
def confirm_artifact(
    artifact_id: str,
    request: Request,
    data: ArtifactConfirmRequest | None = None,
    db: Session = Depends(get_db),
):
    """人工二次确认。高危制品必须先有 dry-run 差异。"""
    # 主体以服务端解析的为准，请求体里的 operator 只在未配置 principals 时兜底。
    # 制品是「谁拍的板」的唯一记录，这一格由客户端自报就等于没记。
    operator = principal_label(db, request) or (data.operator if data else None)
    artifact = _guard(
        lambda: agent_pipeline.confirm(db, artifact_id, operator=operator)
    )
    return _to_out(artifact)


@router.post(
    "/agents/artifacts/{artifact_id}/agent-approval",
    response_model=GovernanceArtifactOut,
)
def set_artifact_agent_approval(
    artifact_id: str,
    request: Request,
    data: ArtifactAgentApprovalRequest,
    db: Session = Depends(get_db),
):
    """逐条给出/收回「允许外部 agent 代执行这条任务」。

    **这是这道闸的唯一写入口，且刻意只开在 REST 上**：MCP 工具只读它。放到 MCP 上
    等于让 agent 自己给自己发许可，闸门就不存在了。

    角色回答的是「这个身份能不能做这类事」，一发长期有效；这里回答的是「这一条现在
    可以让 agent 自己确认并推到远端」。两个问题，两道闸。
    """
    artifact = db.get(GovernanceArtifact, artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    operator = (data.operator or "").strip() or None
    artifact.agent_execution_approved = bool(data.approved)
    if data.approved:
        artifact.agent_execution_approved_by = operator
        artifact.agent_execution_approved_at = datetime.now(UTC).replace(tzinfo=None)
    else:
        # 收回时清掉署名与时间：留着会让人以为授权还在。
        artifact.agent_execution_approved_by = None
        artifact.agent_execution_approved_at = None
    db.commit()
    db.refresh(artifact)
    return _to_out(artifact)


@router.post(
    "/agents/artifacts/{artifact_id}/execute", response_model=GovernanceArtifactOut
)
def execute_artifact(
    artifact_id: str,
    request: Request,
    data: ArtifactExecuteRequest | None = None,
    db: Session = Depends(get_db),
):
    """执行。仅接受 confirmed 状态；已成功的制品重复调用直接返回原回执（幂等）。"""
    context = data.context if data else {}
    artifact = _guard(lambda: agent_pipeline.execute(db, artifact_id, context=context))
    live_state = _try_live_state(db, artifact)
    out = _to_out(artifact, live_state=live_state)
    out.live_state = live_state
    return out


@router.post(
    "/agents/artifacts/{artifact_id}/result", response_model=GovernanceArtifactOut
)
def confirm_artifact_result(
    artifact_id: str,
    request: Request,
    data: ArtifactResultRequest,
    db: Session = Depends(get_db),
):
    """记下人对执行结果的判断：符不符合预期。

    **跑完了不等于跑对了**——``status`` 与回执是系统这侧的事实，这条是人看过之后的判断，
    两者分列。故这里只接受显式的 outcome，永远不从 status 推一个出来。

    只有终态可写（409）。署名取已认证主体，不看请求体。
    """
    artifact = _guard(
        lambda: agent_pipeline.confirm_result(
            db,
            artifact_id,
            outcome=data.outcome,
            note=data.note,
            operator=principal_label(db, request),
            via="frontend",
        )
    )
    return _to_out(artifact)
