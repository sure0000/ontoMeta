"""治理智能体流水线（M5 · 写侧）。

与读侧的 Data Agent（Chat BI）是同一个智能体的两类技能：读侧问数，写侧造数，
共用本体做 grounding、共用 MCP 对外。

权限：本路由整体归 publisher —— 写侧智能体会改集群、建表、执行 SQL，
不能让 editor 触碰（策略见 ``auth._ROLE_OVERRIDES``）。
"""

import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.agents import registry
from app.api.deps import agent_pipeline, task_pipeline
from app.database import get_db
from app.models.agent import HIGH_RISK_KINDS, ArtifactStatus, GovernanceArtifact
from app.schemas.chat_bi import ChatBiFormRequest
from app.schemas import (
    AgentKindsOut,
    ArtifactConfirmRequest,
    ArtifactDraftRequest,
    ConfirmedArtifactDraftRequest,
    ArtifactEditRequest,
    ArtifactExecuteRequest,
    GovernanceArtifactOut,
    PipelineAdvanceConfirmedRequest,
    PipelineCompileOut,
    PipelineScheduleRequest,
    TaskFormRequest,
    TaskPipelineAdvanceOut,
    TaskPipelineCreateRequest,
    TaskPipelineDraftAllOut,
    TaskPipelineOut,
)
from app.services.agent_pipeline import PipelineError
from app.services.chat_bi_tool_schemas import _ACTION_KIND_LABEL
from app.services import chat_bi_ledger

router = APIRouter()


def _record_artifact_decision(
    db: Session,
    request: Request,
    artifact: GovernanceArtifact,
    *,
    node: str,
    stage: str,
    trigger: str,
    summary: str,
    outcome: str | None = None,
    chosen: dict | None = None,
) -> None:
    """把制品的确认/执行记进催生它的会话的决策留痕。

    **记录点放 API 层而非 agent_pipeline 服务层**，有两个理由：
    ① ``agent_pipeline`` 必须对 chat 无感——制品的权威在 agent 侧，对话只是入口之一，
       制品也能从工单表单直接起草；服务层依赖会话会把方向弄反。
    ② ``request.state`` 里的已认证主体只在 API 层可得。
    与 ``record_domain_memory`` 由 API 层而非 ``ask()`` 调用完全同构。

    制品无对应会话（工单直接起草）时静默跳过——不是错误，是正常路径。
    """
    conversation_id = chat_bi_ledger.resolve_conversation_for_artifact(db, artifact.id)
    if not conversation_id:
        return
    chat_bi_ledger.safe_record(
        db,
        conversation_id=conversation_id,
        node=node,
        stage=stage,
        trigger=trigger,
        outcome=outcome,
        summary=summary,
        chosen=chosen,
        ref_kind="artifact",
        ref_id=artifact.id,
        subject_id=getattr(request.state, "principal_id", None),
        subject_role=getattr(request.state, "principal_role", None),
        dedup_key=f"{conversation_id}:{node}:{stage}:{artifact.id}",
    )


def _loads(raw: str | None):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None


def _to_out(a: GovernanceArtifact, *, live_state: dict | None = None) -> GovernanceArtifactOut:
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
        spec=_loads(a.spec_json) or {},
        status=effective_status,
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
    # P0a：列表端点也回读 live_state（只对 materialize，其他类型不需要轮询 Airflow）
    out = []
    for a in rows:
        ls = _try_live_state(db, a) if a.kind == "materialize" else None
        item = _to_out(a, live_state=ls)
        item.live_state = ls
        out.append(item)
    return out


@router.get("/agents/artifacts/{artifact_id}", response_model=GovernanceArtifactOut)
def get_artifact(artifact_id: str, db: Session = Depends(get_db)):
    artifact = agent_pipeline.get(db, artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="制品不存在")
    # P1-6：best-effort 回读 DagRun 实时态（失败退制品 status，复用 chat_bi._live_task_state 逻辑）
    ls = _try_live_state(db, artifact)
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
def draft_artifact(data: ArtifactDraftRequest, db: Session = Depends(get_db)):
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
        )
    )
    return _to_out(artifact)


@router.post("/agents/task-form", response_model=ChatBiFormRequest)
def task_confirmation_form(data: TaskFormRequest, db: Session = Depends(get_db)):
    """按任务类型现取一张**六环确认表单**（字段骨架 + 真实候选 + 本次 confirmation_id）。

    对话里的 ``request_form`` 是模型触发的同一张表；这个端点给**非模型触发**的入口用
    ——任务链要逐步确认时，第 N 步也得拿到和单发任务一模一样的向导，否则「链上的任务
    可以少确认几环」就成了事实上的旁路。
    """
    from app.api.deps import chat_bi_service
    from app.models import Ontology

    if data.kind not in registry.registered_kinds():
        raise HTTPException(status_code=400, detail=f"未知任务类型：{data.kind}")
    if db.get(Ontology, data.ontology_id) is None:
        raise HTTPException(status_code=404, detail="本体不存在")
    form = _guard(
        lambda: chat_bi_service.build_task_form(
            db,
            kind=data.kind,
            ontology_id=data.ontology_id,
            title=data.title or f"确认{_ACTION_KIND_LABEL.get(data.kind, data.kind)}任务",
            intent=data.intent,
            prefill=data.prefill,
        )
    )
    return ChatBiFormRequest(**{k: v for k, v in form.items() if k != "prefilled"})


@router.post("/agents/draft-confirmed", response_model=GovernanceArtifactOut)
def draft_confirmed_artifact(
    data: ConfirmedArtifactDraftRequest,
    db: Session = Depends(get_db),
):
    """前三环确认（需求/本体/数据）走完 → 草稿 + dry-run；不再发起第二轮 LLM。

    ``_dispatch_propose_action`` 会核对 confirmation_id 对应的 requirement/ontology/data
    三条记录，并以人的 chosen 覆盖请求 context。随后建立会话关联，再产出执行方案预览——
    后三环（执行方案 / 执行 / 结果）由人在任务详情里各自确认。
    """
    from app.api.deps import chat_bi_service
    from app.models import Ontology

    conversation = chat_bi_service.get_conversation(db, data.conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="对话不存在")

    ontology = db.get(Ontology, data.ontology_id)
    if ontology is None:
        raise HTTPException(status_code=404, detail="本体不存在")
    conversation_domain_ids = set(conversation.domain_ids)
    if (
        conversation_domain_ids
        and ontology.domain_context_id not in conversation_domain_ids
    ):
        raise HTTPException(status_code=409, detail="本体不属于当前会话的数据域作用域")

    proposal, _summary, is_error = chat_bi_service._dispatch_propose_action(
        db,
        ontology_id=data.ontology_id,
        domain_id=ontology.domain_context_id,
        conversation_id=data.conversation_id,
        args={
            "kind": data.kind,
            "intent": data.intent,
            "context": {
                **data.context,
                "task_confirmation_id": data.confirmation_id,
            },
        },
    )
    if is_error:
        raise HTTPException(status_code=409, detail=proposal.get("error") or "任务确认不完整")

    payload = proposal["draft_payload"]
    artifact = _guard(
        lambda: agent_pipeline.draft(
            db,
            kind=payload["kind"],
            intent=payload["intent"],
            context={
                "ontology_id": payload["ontology_id"],
                **payload["context"],
            },
            ontology_id=payload["ontology_id"],
            user_created=True,
        )
    )
    from app.api.deps import chat_bi_service as chat_service

    chat_service.link_conversation_task(
        db,
        data.conversation_id,
        artifact.id,
        kind=artifact.kind,
        intent=artifact.intent,
        # 前三环是按这张表单的 confirmation_id 记的账；不把它落到关联上，这条任务的
        # 闭环就只剩后三环，前三环明明确认过却在界面上恒灰。
        confirmation_id=data.confirmation_id,
    )
    artifact = _guard(lambda: agent_pipeline.validate(db, artifact.id, context={}))
    return _to_out(artifact)


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
    operator = data.operator if data else None
    artifact = _guard(
        lambda: agent_pipeline.confirm(db, artifact_id, operator=operator)
    )
    _record_artifact_decision(
        db,
        request,
        artifact,
        node="plan",
        stage="artifact_confirm",
        trigger="artifact_confirm",
        summary=f"确认「{artifact.name or artifact.kind}」可执行",
    )
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
    _record_artifact_decision(
        db,
        request,
        artifact,
        node="execute",
        stage="artifact_execute",
        trigger="artifact_execute",
        summary=f"执行「{artifact.name or artifact.kind}」：{artifact.status}",
        # 执行成功与否是事实而非人的选择，但它决定这一环算不算走通，故落进 outcome。
        outcome=(
            "rejected" if artifact.status == ArtifactStatus.FAILED.value else "accepted"
        ),
        chosen={"status": artifact.status},
    )
    live_state = _try_live_state(db, artifact)
    out = _to_out(artifact, live_state=live_state)
    out.live_state = live_state
    return out


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


@router.post(
    "/agents/pipelines/{pipeline_id}/advance-confirmed",
    response_model=TaskPipelineAdvanceOut,
)
def advance_pipeline_confirmed(
    pipeline_id: str,
    data: PipelineAdvanceConfirmedRequest,
    db: Session = Depends(get_db),
):
    """确认过前三环后起草链上的下一步，并直接产出执行方案预览（validate + dry-run）。

    与单发任务的 ``/agents/draft-confirmed`` 是同一条规矩：**链不替谁确认**。第 N 步同样
    要人分别确认需求 / 本体 / 数据，才允许起草；执行方案 / 执行 / 结果三环再在任务详情里
    各自确认。缺任何一环都 409 并说清缺哪环——含糊成"不能推进"等于没说。
    """
    from app.api.deps import chat_bi_service

    conversation = chat_bi_service.get_conversation(db, data.conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="对话不存在")

    missing = chat_bi_ledger.missing_task_confirmations(
        db, data.conversation_id, data.confirmation_id
    )
    if missing:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "任务链的这一步必须先逐环确认需求、本体/口径和数据落点",
                "missing_confirmations": missing,
                "missing_labels": [chat_bi_ledger.node_label(n) for n in missing],
            },
        )

    # 人在向导里定下的是权威值：写回该步的 context 后再起草，链的继承只补没填的。
    context = {
        str(k): v
        for k, v in (data.context or {}).items()
        if k != "task_confirmation_id" and v is not None
    }
    artifact = _guard(
        lambda: task_pipeline.advance(
            db,
            pipeline_id,
            context=context,
            intent=(data.intent or "").strip() or None,
            user_created=True,
        )
    )
    # 关联必须先建：后三环（plan/execute/result）靠 (会话, 制品) 这条关联记回同一闭环。
    chat_bi_service.link_conversation_task(
        db,
        data.conversation_id,
        artifact.id,
        kind=artifact.kind,
        intent=artifact.intent,
        # 链上的每一步都是一条独立任务、各有自己的六环，故也各带自己的 confirmation_id。
        confirmation_id=data.confirmation_id,
    )
    artifact = _guard(lambda: agent_pipeline.validate(db, artifact.id, context={}))
    return TaskPipelineAdvanceOut(
        pipeline=TaskPipelineOut(**task_pipeline.detail(db, pipeline_id)),
        artifact=_to_out(artifact),
    )


@router.post(
    "/agents/pipelines/{pipeline_id}/draft-all", response_model=TaskPipelineDraftAllOut
)
def draft_all_pipeline(pipeline_id: str, db: Session = Depends(get_db)):
    """C2：一键起草**全部**步骤，返回各步制品。**只起草**。

    与 advance 的区别：不再要求上游执行成功才起草下一步——血缘驱动的依赖
    由各步骤的 depends_on 表达，起草阶段不阻塞（所有制品先落地，人再逐个
    校验/确认/执行；执行顺序由血缘决定）。「未确认不得执行」不变量不变。

    **Data Agent 不再暴露这条路径**：它会让链上每一步都跳过需求/本体/数据三环确认，
    与「所有任务逐环确认」冲突。对话入口一律走
    ``/agents/pipelines/{id}/advance-confirmed``（逐步确认后起草）。此端点保留给
    没有对话上下文的运维/编排调用方。
    """
    artifacts = _guard(lambda: task_pipeline.draft_all(db, pipeline_id))
    return TaskPipelineDraftAllOut(
        pipeline=TaskPipelineOut(**task_pipeline.detail(db, pipeline_id)),
        artifacts=[_to_out(a) for a in artifacts],
    )


@router.put("/agents/pipelines/{pipeline_id}/schedule", response_model=TaskPipelineOut)
def set_pipeline_schedule(
    pipeline_id: str, data: PipelineScheduleRequest, db: Session = Depends(get_db)
):
    """给链设置周期调度 cron。设置后即可编译成周期 DAG。

    只落 cron 到链上，不触发编译——编译是显式的第二步（要校验所有步骤已确认）。
    """
    def _set():
        pipeline = task_pipeline.require(db, pipeline_id)
        pipeline.schedule_cron = (data.schedule_cron or "").strip() or None
        db.commit()
        return task_pipeline.detail(db, pipeline_id)

    return TaskPipelineOut(**_guard(_set))


@router.post("/agents/pipelines/{pipeline_id}/compile", response_model=PipelineCompileOut)
def compile_pipeline_endpoint(pipeline_id: str, db: Session = Depends(get_db)):
    """把链编译成一条周期 DAG。

    **编译前提**（不满足则 409/400）：所有步骤已确认、已执行过一次、spec 未在确认后变更。
    周期调度天然是「无人值守反复执行」，与「每次执行都要人确认」冲突——折中是把确认前移：
    人确认的是「这条链的这个版本可以反复跑」，编译时把各步 spec 快照进 DAG。
    """
    from app.services.pipeline_compiler import PipelineCompileError, compile_pipeline

    def _compile():
        try:
            return compile_pipeline(db, pipeline_id)
        except PipelineCompileError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    result = _guard(_compile)
    return PipelineCompileOut(**result)


@router.delete("/agents/pipelines/{pipeline_id}/schedule", response_model=TaskPipelineOut)
def unschedule_pipeline(pipeline_id: str, db: Session = Depends(get_db)):
    """下线周期调度：清 schedule_cron 与 compiled_dag_id。

    注意：这只清 ontoMeta 侧的记录。已落盘的 DAG 文件需另行删除（或让 Airflow 停用）——
    ontoMeta 不直接删 Airflow 的 DAG 文件，避免误删部署方手动接管的东西。
    """
    def _unschedule():
        pipeline = task_pipeline.require(db, pipeline_id)
        pipeline.schedule_cron = None
        pipeline.compiled_dag_id = None
        pipeline.compiled_at = None
        db.commit()
        return task_pipeline.detail(db, pipeline_id)

    return TaskPipelineOut(**_guard(_unschedule))


@router.get("/agents/pipelines/{pipeline_id}/lineage")
def preview_pipeline_lineage(pipeline_id: str, db: Session = Depends(get_db)):
    """预览链级血缘边（P3-3）：串联各步产出表（物化→清洗→聚合）。纯读，不触碰 DataHub。"""
    from app.services.pipeline_lineage import PipelineLineageEmitter

    return _guard(lambda: PipelineLineageEmitter().preview(db, pipeline_id))


@router.post("/agents/pipelines/{pipeline_id}/lineage")
def apply_pipeline_lineage(pipeline_id: str, db: Session = Depends(get_db)):
    """上报链级血缘到 DataHub（P3-3）。逐条记录失败，不因单条中断整体。"""
    from app.services.pipeline_lineage import PipelineLineageEmitter

    return _guard(lambda: PipelineLineageEmitter().apply_sync(db, pipeline_id))
