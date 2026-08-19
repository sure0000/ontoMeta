import json
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api.deps import chat_bi_service
from app.database import get_db
from app.services import agent_telemetry, chat_bi_ledger
from app.services.chat_bi_blocks import answer_to_blocks
from app.services.data_app_executor import ExecutionError
from app.schemas import (
    ChatBiAnswer,
    ChatBiAskRequest,
    ChatBiCategoryDeleteRequest,
    ChatBiCategoryList,
    ChatBiCategoryRenameRequest,
    ChatBiConversationCreate,
    ChatBiDecisionClosure,
    ChatBiDecisionOut,
    ChatBiDecisionRequest,
    ChatBiExecuteRequest,
    ChatBiExecuteResult,
    ChatBiConversationSummary,
    ChatBiConversationUpdate,
    ChatBiMessageOut,
    ChatBiSuggestions,
    ChatBiTaskLinkRequest,
    ChatBiPreferenceRequest,
)

router = APIRouter()
logger = logging.getLogger("ontometa.chat_bi_api")

@router.get(
    "/chat-bi/conversations", response_model=list[ChatBiConversationSummary]
)
def chat_bi_list_conversations(
    domain_ids: list[str] = Query(default_factory=list),
    q: str | None = Query(None),
    include_archived: bool = Query(False),
    db: Session = Depends(get_db),
):
    return chat_bi_service.list_conversations(
        db, domain_ids, query=q, include_archived=include_archived
    )


@router.post(
    "/chat-bi/conversations", response_model=ChatBiConversationSummary
)
def chat_bi_create_conversation(
    data: ChatBiConversationCreate,
    db: Session = Depends(get_db),
):
    return chat_bi_service.create_conversation(
        db, domain_ids=data.domain_ids, title=data.title, category=data.category
    )


@router.patch(
    "/chat-bi/conversations/{conversation_id}",
    response_model=ChatBiConversationSummary,
)
def chat_bi_update_conversation(
    conversation_id: str,
    data: ChatBiConversationUpdate,
    db: Session = Depends(get_db),
):
    try:
        update_data = data.model_dump(exclude_unset=True)
        kwargs: dict = {}
        if "title" in update_data:
            kwargs["title"] = update_data["title"]
        if "category" in update_data:
            kwargs["category"] = update_data["category"]
        if "is_pinned" in update_data:
            kwargs["is_pinned"] = update_data["is_pinned"]
        if "is_archived" in update_data:
            kwargs["is_archived"] = update_data["is_archived"]
        return chat_bi_service.update_conversation(
            db, conversation_id, **kwargs
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/chat-bi/conversations/{conversation_id}")
def chat_bi_delete_conversation(
    conversation_id: str,
    db: Session = Depends(get_db),
):
    try:
        chat_bi_service.delete_conversation(db, conversation_id)
        return {"id": conversation_id, "deleted": True}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/chat-bi/conversations/{conversation_id}/tasks")
def chat_bi_link_conversation_task(
    conversation_id: str,
    data: ChatBiTaskLinkRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """P1：记录「本会话催生了某数据任务（治理制品）」。

    前端在用户对任务提案点「去校验并执行」建出制品后调用；使该会话后续能免 id 追踪任务。

    顺带记一条「执行方案确认」决策留痕：提案原样 vs 人改后的参数都在这一次请求里，
    不必再多一次往返。留痕失败不影响关联本身（record_decision 自身吞异常）。
    """
    try:
        result = chat_bi_service.link_conversation_task(
            db, conversation_id, data.artifact_id, kind=data.kind, intent=data.intent
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    chat_bi_ledger.safe_record(
        db,
        conversation_id=conversation_id,
        node="plan",
        stage="proposal_draft",
        trigger="action_proposal_draft",
        message_id=data.message_id,
        block_id=data.block_id,
        summary=data.intent or f"确认{data.kind or ''}任务方案",
        proposed=data.proposed_context,
        chosen=data.chosen_context,
        ref_kind="artifact",
        ref_id=data.artifact_id,
        subject_id=_principal_id(request),
        subject_role=_principal_role(request),
        dedup_key=f"{conversation_id}:plan:draft:{data.artifact_id}",
    )
    return result


@router.post("/chat-bi/conversations/{conversation_id}/decisions")
def chat_bi_record_decision(
    conversation_id: str,
    data: ChatBiDecisionRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """记一条人工决策留痕。

    **永远返回 200**（``recorded=false`` 表未记成），不返回 4xx/5xx：前端是
    fire-and-forget 调用，返回错误码只会在控制台刷红而没人能处理，更不该让
    留痕失败连累用户正在做的确认动作。
    """
    decision_id = chat_bi_ledger.safe_record(
        db,
        conversation_id=conversation_id,
        node=data.node,
        outcome=data.outcome,
        stage=data.stage,
        trigger=data.trigger,
        message_id=data.message_id,
        block_id=data.block_id,
        summary=data.summary,
        proposed=data.proposed,
        chosen=data.chosen,
        ref_kind=data.ref_kind,
        ref_id=data.ref_id,
        # 责任人只认已认证主体，请求体里给什么都不看。
        subject_id=_principal_id(request),
        subject_role=_principal_role(request),
        dedup_key=data.dedup_key,
    )
    return {"id": decision_id, "recorded": decision_id is not None}


@router.get(
    "/chat-bi/conversations/{conversation_id}/decisions",
    response_model=list[ChatBiDecisionOut],
)
def chat_bi_list_decisions(conversation_id: str, db: Session = Depends(get_db)):
    """本会话的决策时间线（最早在前）。"""
    return chat_bi_ledger.list_decisions(db, conversation_id)


@router.get(
    "/chat-bi/conversations/{conversation_id}/closure",
    response_model=ChatBiDecisionClosure,
)
def chat_bi_decision_closure(conversation_id: str, db: Session = Depends(get_db)):
    """一次对话的确认闭环总结：恒六环 + 悬挂项 + 完整时间线。"""
    return chat_bi_ledger.build_closure(db, conversation_id)


@router.get("/chat-bi/decisions", response_model=list[ChatBiDecisionOut])
def chat_bi_search_decisions(
    node: str | None = None,
    outcome: str | None = None,
    ref_kind: str | None = None,
    subject_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    """跨会话决策查询，供决策追踪页。"""
    return chat_bi_ledger.search_decisions(
        db,
        node=node,
        outcome=outcome,
        ref_kind=ref_kind,
        subject_id=subject_id,
        since=since,
        until=until,
        limit=limit,
    )


@router.post("/chat-bi/domain-memory/preferences")
def chat_bi_remember_preference(
    data: ChatBiPreferenceRequest,
    db: Session = Depends(get_db),
):
    """P3.1：把用户确认的约定落库为本域记忆（前端在用户对记忆提案点「记住」后调用）。"""
    try:
        return chat_bi_service.record_domain_preference(db, data.domain_id, data.text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/chat-bi/categories", response_model=ChatBiCategoryList)
def chat_bi_list_categories(
    domain_ids: list[str] = Query(default_factory=list),
    db: Session = Depends(get_db),
):
    categories = chat_bi_service.list_categories(db, domain_ids)
    return ChatBiCategoryList(categories=categories)


@router.post("/chat-bi/categories/rename")
def chat_bi_rename_category(
    data: ChatBiCategoryRenameRequest,
    db: Session = Depends(get_db),
):
    try:
        chat_bi_service.rename_category(
            db, domain_ids=data.domain_ids, old_name=data.old_name, new_name=data.new_name
        )
        return {"success": True}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/chat-bi/categories/delete")
def chat_bi_delete_category(
    data: ChatBiCategoryDeleteRequest,
    db: Session = Depends(get_db),
):
    chat_bi_service.delete_category(db, domain_ids=data.domain_ids, name=data.name)
    return {"success": True}


@router.get(
    "/chat-bi/conversations/{conversation_id}/messages",
    response_model=list[ChatBiMessageOut],
)
def chat_bi_get_messages(
    conversation_id: str,
    db: Session = Depends(get_db),
):
    conv = chat_bi_service.get_conversation(db, conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="对话不存在")
    return chat_bi_service.get_messages(db, conversation_id)


# ---- Ask


def _principal_role(request: Request) -> str | None:
    """当前请求主体的角色（由 AdminAuthMiddleware 写入 request.state）。

    Agent 的 run_sql 按此做工具粒度授权（P1.1）——不能只靠端点粒度，
    否则 editor 自己执行 SQL 被 403、让 Agent 代跑却放行。
    """
    return getattr(request.state, "principal_role", None)


def _principal_id(request: Request) -> str | None:
    """当前请求主体的 id（由 AdminAuthMiddleware 写入 request.state）。

    决策留痕的责任人**必须**由这里取，不能信前端请求体里的 operator——否则
    「谁确认的」可被客户端随意伪造，追踪与管理就失去依据。

    用共享 ``ONTOMETA_ADMIN_TOKEN`` 访问时 ``resolve_principal`` 不查库、返回 None，
    此时只有角色可考；要精确到人需先把 Principal 逐人配起来（运营前提，非代码问题）。
    """
    return getattr(request.state, "principal_id", None)


@router.post("/chat-bi/ask", response_model=ChatBiAnswer)
async def chat_bi_ask(
    data: ChatBiAskRequest, request: Request, db: Session = Depends(get_db)
):
    try:
        conversation_id = data.conversation_id

        if conversation_id:
            conv = chat_bi_service.get_conversation(db, conversation_id)
            if not conv:
                raise HTTPException(status_code=404, detail="对话不存在")
            if set(conv.domain_ids) != set(data.domain_ids):
                raise HTTPException(
                    status_code=400,
                    detail="会话不属于当前数据域作用域，请切换到正确作用域或新建会话",
                )
            conversation_title = conv.title
        else:
            conv_dict = chat_bi_service.create_conversation(
                db, domain_ids=data.domain_ids, title=data.question[:50]
            )
            conversation_id = conv_dict["id"]
            conversation_title = conv_dict["title"]

        chat_bi_service.save_message(
            db, conversation_id, "user", data.question
        )

        payload = await chat_bi_service.ask(
            db,
            domain_ids=data.domain_ids,
            question=data.question,
            history=data.history,
            principal_role=_principal_role(request),
            conversation_id=conversation_id,
        )

        # V3 S0：终态 payload 投影成渲染块（双写，旧字段保留）。落库与返回都含 blocks。
        payload["blocks"] = answer_to_blocks(payload)

        chat_bi_service.save_message(
            db,
            conversation_id,
            "assistant",
            payload["answer"],
            payload={
                k: v
                for k, v in payload.items()
                if k not in ("domain_id", "domain_name", "domain_ids", "domain_names")
            },
        )

        # P3：跨会话记忆——把本次已接地命中的对象/口径按各涉及域累加使用度（best-effort）。
        chat_bi_service.record_domain_memory(db, data.domain_ids, payload)

        payload["conversation_id"] = conversation_id
        payload["conversation_title"] = conversation_title
        return payload
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/chat-bi/ask/stream")
async def chat_bi_ask_stream(
    data: ChatBiAskRequest, request: Request, db: Session = Depends(get_db)
):
    """SSE 流式问答：实时推送 agent 工具步骤与逐字答案。

    事件（`data: {json}\\n\\n`）：
    meta / step_start / step_done / thought / repair / token / done / error。
    ``repair``（P4.3）表示答案未过可靠性校验、正在让模型重写一次。
    会话创建与 user 消息在流开始前落库；assistant 消息在 done 后落库。
    """
    conversation_id = data.conversation_id
    if conversation_id:
        conv = chat_bi_service.get_conversation(db, conversation_id)
        if not conv:
            raise HTTPException(status_code=404, detail="对话不存在")
        if set(conv.domain_ids) != set(data.domain_ids):
            raise HTTPException(
                status_code=400,
                detail="会话不属于当前数据域作用域，请切换到正确作用域或新建会话",
            )
        conversation_title = conv.title
    else:
        conv_dict = chat_bi_service.create_conversation(
            db, domain_ids=data.domain_ids, title=data.question[:50]
        )
        conversation_id = conv_dict["id"]
        conversation_title = conv_dict["title"]

    chat_bi_service.save_message(db, conversation_id, "user", data.question)

    def sse(event: dict) -> str:
        return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    async def event_gen():
        yield sse({
            "type": "meta",
            "conversation_id": conversation_id,
            "conversation_title": conversation_title,
        })
        try:
            async for ev in chat_bi_service.ask_stream(
                db,
                domain_ids=data.domain_ids,
                question=data.question,
                history=data.history,
                principal_role=_principal_role(request),
                conversation_id=conversation_id,
            ):
                if ev.get("type") == "done":
                    payload = ev["payload"]
                    # V3 S0：终态 payload 投影成渲染块（双写）。SSE 下发与落库都含 blocks。
                    payload["blocks"] = answer_to_blocks(payload)
                    payload["conversation_id"] = conversation_id
                    payload["conversation_title"] = conversation_title
                    yield sse(ev)
                    chat_bi_service.save_message(
                        db,
                        conversation_id,
                        "assistant",
                        payload.get("answer", ""),
                        payload={
                            k: v for k, v in payload.items()
                            if k not in ("domain_id", "domain_name", "domain_ids", "domain_names")
                        },
                    )
                    # P3：跨会话记忆——本次已接地命中的对象/口径按各涉及域累加使用度（best-effort）。
                    chat_bi_service.record_domain_memory(db, data.domain_ids, payload)
                else:
                    yield sse(ev)
        except ValueError as exc:
            yield sse({"type": "error", "message": str(exc)})
        except Exception:  # noqa: BLE001
            logger.exception("ChatBI stream endpoint failed")
            yield sse({"type": "error", "message": "服务异常，请重试"})

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 禁 nginx 缓冲，确保逐块下发
        },
    )


@router.get("/chat-bi/telemetry")
def chat_bi_telemetry():
    """Data Agent 改造期遥测快照（P0）：步数 / 工具分布 / 拒绝码 / LLM 调用次数。

    进程内计数器，重启即清零——它是 DATA_AGENT_V2_PLAN 各期的**对照基线**，
    不是生产可观测性。改造收尾后可整体摘除。
    """
    return agent_telemetry.snapshot()


@router.get("/chat-bi/suggestions", response_model=ChatBiSuggestions)
def chat_bi_suggestions(
    domain_ids: list[str] = Query(default_factory=list), db: Session = Depends(get_db)
):
    try:
        suggestions = chat_bi_service.suggest_questions(db, domain_ids)
        return ChatBiSuggestions(domain_ids=domain_ids, suggestions=suggestions)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post(
    "/chat-bi/messages/{message_id}/execute", response_model=ChatBiExecuteResult
)
def chat_bi_execute_message(
    message_id: str,
    data: ChatBiExecuteRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """执行该条回答的 suggested_sql，返回真实数据。

    数仓表由本体生成后，SQL 里的表名/列名与本体标识符天然一致——
    问数准确性由架构保证，而非靠提示词约束。
    权限与 run_sql 同一道门：需 publisher 及以上（工具粒度授权）。
    """
    try:
        return chat_bi_service.execute_message_sql(
            db, message_id, data_source_id=data.data_source_id, limit=data.limit,
            principal_role=_principal_role(request),
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ExecutionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
