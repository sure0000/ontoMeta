from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import chat_bi_service
from app.database import get_db
from app.schemas import (
    ChatBiAnswer,
    ChatBiAskRequest,
    ChatBiCategoryDeleteRequest,
    ChatBiCategoryList,
    ChatBiCategoryRenameRequest,
    ChatBiConversationCreate,
    ChatBiConversationSummary,
    ChatBiConversationUpdate,
    ChatBiMessageOut,
    ChatBiSuggestions,
)

router = APIRouter()

@router.get(
    "/chat-bi/conversations", response_model=list[ChatBiConversationSummary]
)
def chat_bi_list_conversations(
    domain_id: str = Query(...),
    q: str | None = Query(None),
    include_archived: bool = Query(False),
    db: Session = Depends(get_db),
):
    return chat_bi_service.list_conversations(
        db, domain_id, query=q, include_archived=include_archived
    )


@router.post(
    "/chat-bi/conversations", response_model=ChatBiConversationSummary
)
def chat_bi_create_conversation(
    data: ChatBiConversationCreate,
    db: Session = Depends(get_db),
):
    return chat_bi_service.create_conversation(
        db, domain_id=data.domain_id, title=data.title, category=data.category
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


@router.get("/chat-bi/categories", response_model=ChatBiCategoryList)
def chat_bi_list_categories(
    domain_id: str = Query(...),
    db: Session = Depends(get_db),
):
    categories = chat_bi_service.list_categories(db, domain_id)
    return ChatBiCategoryList(categories=categories)


@router.post("/chat-bi/categories/rename")
def chat_bi_rename_category(
    data: ChatBiCategoryRenameRequest,
    db: Session = Depends(get_db),
):
    try:
        chat_bi_service.rename_category(
            db, domain_id=data.domain_id, old_name=data.old_name, new_name=data.new_name
        )
        return {"success": True}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/chat-bi/categories/delete")
def chat_bi_delete_category(
    data: ChatBiCategoryDeleteRequest,
    db: Session = Depends(get_db),
):
    chat_bi_service.delete_category(db, domain_id=data.domain_id, name=data.name)
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


@router.post("/chat-bi/ask", response_model=ChatBiAnswer)
async def chat_bi_ask(data: ChatBiAskRequest, db: Session = Depends(get_db)):
    try:
        conversation_id = data.conversation_id

        if conversation_id:
            conv = chat_bi_service.get_conversation(db, conversation_id)
            if not conv:
                raise HTTPException(status_code=404, detail="对话不存在")
            if conv.domain_id != data.domain_id:
                raise HTTPException(
                    status_code=400,
                    detail="会话不属于当前数据域，请切换到正确数据域或新建会话",
                )
            conversation_title = conv.title
        else:
            conv_dict = chat_bi_service.create_conversation(
                db, domain_id=data.domain_id, title=data.question[:50]
            )
            conversation_id = conv_dict["id"]
            conversation_title = conv_dict["title"]

        chat_bi_service.save_message(
            db, conversation_id, "user", data.question
        )

        payload = await chat_bi_service.ask(
            db,
            domain_id=data.domain_id,
            question=data.question,
            history=data.history,
        )

        chat_bi_service.save_message(
            db,
            conversation_id,
            "assistant",
            payload["answer"],
            payload={
                k: v
                for k, v in payload.items()
                if k not in ("domain_id", "domain_name")
            },
        )

        payload["conversation_id"] = conversation_id
        payload["conversation_title"] = conversation_title
        return payload
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/chat-bi/suggestions", response_model=ChatBiSuggestions)
def chat_bi_suggestions(domain_id: str = Query(...), db: Session = Depends(get_db)):
    try:
        suggestions = chat_bi_service.suggest_questions(db, domain_id)
        return ChatBiSuggestions(domain_id=domain_id, suggestions=suggestions)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
