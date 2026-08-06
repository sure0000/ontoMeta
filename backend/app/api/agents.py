"""治理智能体流水线（M5 · 写侧）。

与读侧的 Data Agent（Chat BI）是同一个智能体的两类技能：读侧问数，写侧造数，
共用本体做 grounding、共用 MCP 对外。

权限：本路由整体归 publisher —— 写侧智能体会改集群、建表、执行 SQL，
不能让 editor 触碰（策略见 ``auth._ROLE_OVERRIDES``）。
"""

import json

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.agents import registry
from app.api.deps import agent_pipeline, task_pipeline
from app.database import get_db
from app.models.agent import HIGH_RISK_KINDS, GovernanceArtifact
from app.schemas import (
    AgentKindsOut,
    ArtifactConfirmRequest,
    ArtifactDraftRequest,
    ArtifactExecuteRequest,
    GovernanceArtifactOut,
    TaskPipelineAdvanceOut,
    TaskPipelineCreateRequest,
    TaskPipelineOut,
)
from app.services.agent_pipeline import PipelineError

router = APIRouter()


def _loads(raw: str | None):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None


def _to_out(a: GovernanceArtifact) -> GovernanceArtifactOut:
    return GovernanceArtifactOut(
        id=a.id,
        kind=a.kind,
        name=a.name,
        ontology_id=a.ontology_id,
        intent=a.intent,
        spec=_loads(a.spec_json) or {},
        status=a.status,
        is_high_risk=a.is_high_risk,
        validation_report=_loads(a.validation_report_json),
        execution_receipt=_loads(a.execution_receipt_json),
        confirmed_by=a.confirmed_by,
        confirmed_at=a.confirmed_at,
        executed_at=a.executed_at,
        origin=a.origin,
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
    db: Session = Depends(get_db),
):
    rows = agent_pipeline.list_artifacts(
        db, kind=kind, status=status, ontology_id=ontology_id
    )
    return [_to_out(a) for a in rows]


@router.get("/agents/artifacts/{artifact_id}", response_model=GovernanceArtifactOut)
def get_artifact(artifact_id: str, db: Session = Depends(get_db)):
    artifact = agent_pipeline.get(db, artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="制品不存在")
    return _to_out(artifact)


@router.post("/agents/draft", response_model=GovernanceArtifactOut)
def draft_artifact(data: ArtifactDraftRequest, db: Session = Depends(get_db)):
    """自然语言意图 → 声明式 Spec。LLM 只产规格，不产命令。"""
    artifact = _guard(
        lambda: agent_pipeline.draft(
            db,
            kind=data.kind,
            intent=data.intent,
            context=data.context,
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
    data: ArtifactConfirmRequest | None = None,
    db: Session = Depends(get_db),
):
    """人工二次确认。高危制品必须先有 dry-run 差异。"""
    operator = data.operator if data else None
    artifact = _guard(
        lambda: agent_pipeline.confirm(db, artifact_id, operator=operator)
    )
    return _to_out(artifact)


@router.post(
    "/agents/artifacts/{artifact_id}/execute", response_model=GovernanceArtifactOut
)
def execute_artifact(
    artifact_id: str,
    data: ArtifactExecuteRequest | None = None,
    db: Session = Depends(get_db),
):
    """执行。仅接受 confirmed 状态；已成功的制品重复调用直接返回原回执（幂等）。"""
    context = data.context if data else {}
    artifact = _guard(lambda: agent_pipeline.execute(db, artifact_id, context=context))
    return _to_out(artifact)


# ---------------- 任务链（多任务编排） ----------------
#
# 链只管顺序与上下文传递：每一步仍是一条独立制品，照旧各自走上面那套
# validate → confirm → execute。故这里**没有**「执行整条链」的端点——那会绕过逐制品的
# 人工确认，而「未确认不得执行」是这条流水线的硬不变量。


@router.post("/agents/pipelines", response_model=TaskPipelineOut)
def create_pipeline(data: TaskPipelineCreateRequest, db: Session = Depends(get_db)):
    """建一条任务链（如 物化 → 清洗 → 聚合）。只落意图，不起草任何制品。"""
    pipeline = _guard(
        lambda: task_pipeline.create(
            db,
            name=data.name,
            intent=data.intent,
            ontology_id=data.ontology_id,
            steps=[s.model_dump() for s in data.steps],
        )
    )
    return TaskPipelineOut(**task_pipeline.detail(db, pipeline.id))


@router.get("/agents/pipelines", response_model=list[TaskPipelineOut])
def list_pipelines(
    ontology_id: str | None = Query(None), db: Session = Depends(get_db)
):
    rows = task_pipeline.list_pipelines(db, ontology_id=ontology_id)
    return [TaskPipelineOut(**task_pipeline.detail(db, p.id)) for p in rows]


@router.get("/agents/pipelines/{pipeline_id}", response_model=TaskPipelineOut)
def get_pipeline(pipeline_id: str, db: Session = Depends(get_db)):
    return TaskPipelineOut(**_guard(lambda: task_pipeline.detail(db, pipeline_id)))


@router.post("/agents/pipelines/{pipeline_id}/advance", response_model=TaskPipelineAdvanceOut)
def advance_pipeline(pipeline_id: str, db: Session = Depends(get_db)):
    """起草链上的下一步，返回它的制品。**只起草**——校验/确认/执行仍走各自的端点。

    上游还没执行成功时拒绝（409），并说清卡在哪一步：这两种情形对用户意味着完全不同的
    下一步动作，含糊成一句「不能推进」等于没说。
    """
    artifact = _guard(lambda: task_pipeline.advance(db, pipeline_id))
    return TaskPipelineAdvanceOut(
        pipeline=TaskPipelineOut(**task_pipeline.detail(db, pipeline_id)),
        artifact=_to_out(artifact),
    )
