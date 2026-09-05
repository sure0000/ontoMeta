"""一次性网页表单的存取：交互式建数流程在「客户端没有交互工具」时的兜底面。

分工写在这里，免得两边各自演化：

- **同一份表单定义**。页面渲染的字段、候选、预填值全部来自 ``tools.flow._plan``——
  和宿主表单、和 Web 的 Data Agent 向导是同一份，不在这里另拼一套。
- **候选不落快照**。表里只存这一环的入参（kind / 本体 / 已有答案），字段与候选每次打开
  实时算：存快照会让人在一张过期的表单上做决定。
- **提交即校验**。提交后重算一遍：这一环还缺格子（比如装载方式刚从全量改成增量，长出了
  主键/增量字段/初始水位）就把表单原地退回继续填，只有真填齐了才落 submitted。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.models.mcp_flow_form import McpFlowForm

#: 表单默认有效期。链接落在对话里，过期比长期可点更安全。
_TTL_MINUTES = 120
#: 网页 Select 自带搜索，候选给全（宿主表单/文本清单才需要截断）。
_WEB_OPTION_LIMIT = 2000


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _plan_for(db: Session, form: McpFlowForm, answers: dict[str, Any]) -> dict[str, Any]:
    from app.mcp.tools.flow import _plan

    return _plan(
        db,
        kind=form.kind,
        answers=answers,
        goal=form.goal or "",
        option_limit=_WEB_OPTION_LIMIT,
    )


def _answers(form: McpFlowForm) -> dict[str, Any]:
    try:
        value = json.loads(form.submitted_json or form.answers_json or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def create_form(
    db: Session,
    *,
    kind: str,
    stage: str,
    answers: dict[str, Any],
    goal: str = "",
    ontology_id: str | None = None,
    created_by: str | None = None,
) -> McpFlowForm:
    form = McpFlowForm(
        kind=kind,
        stage=stage,
        ontology_id=ontology_id,
        goal=goal or "",
        answers_json=json.dumps(answers, ensure_ascii=False, default=str),
        status="pending",
        created_by=created_by,
        expires_at=_now() + timedelta(minutes=_TTL_MINUTES),
    )
    db.add(form)
    db.commit()
    db.refresh(form)
    return form


def get_form(db: Session, form_id: str) -> McpFlowForm | None:
    return db.get(McpFlowForm, form_id)


def form_state(db: Session, form: McpFlowForm) -> dict[str, Any]:
    """给页面渲染用的一份状态：过期 / 已提交 / 待填（含实时算出来的表单）。"""
    base = {
        "id": form.id,
        "kind": form.kind,
        "stage": form.stage,
        "goal": form.goal or "",
        "status": form.status,
        "created_at": form.created_at.isoformat() if form.created_at else None,
        "submitted_at": form.submitted_at.isoformat() if form.submitted_at else None,
        "expires_at": form.expires_at.isoformat() if form.expires_at else None,
    }
    if form.status == "submitted":
        return {**base, "form": None}
    if form.expires_at and form.expires_at < _now():
        return {**base, "status": "expired", "form": None}

    plan = _plan_for(db, form, _answers(form))
    if plan.get("status") == "review":
        return {
            **base,
            "status": "pending",
            "stage": "review",
            "form": plan["form"],
            "review": plan["review"],
        }
    if plan.get("status") != "ask":
        # 参数在别处已经填齐（或被阻断）：这张表单没有可填的东西了，如实说明。
        return {
            **base,
            "status": "stale" if plan.get("status") == "ready" else plan.get("status"),
            "form": None,
            "reason": plan.get("reason"),
        }
    return {**base, "stage": "decide", "form": plan["form"]}


def submit_form(
    db: Session,
    form: McpFlowForm,
    values: dict[str, Any],
    *,
    confirm: bool = False,
    plan_digest: str = "",
) -> dict[str, Any]:
    """把页面填的值并回答案。

    只有**参数填齐且（在执行审查上）人点了确认**才落 submitted，否则原地退回继续填。
    ``plan_digest`` 是页面上显示的那份方案的指纹：对不上说明提交前方案变了（改了参数，
    或数据源那边变了），此时不认这次确认，把新方案退回去让人重看一遍。
    """
    if form.status == "submitted":
        return {**form_state(db, form), "accepted": False, "reason": "这张表单已经提交过了"}
    if form.expires_at and form.expires_at < _now():
        form.status = "expired"
        db.commit()
        return {**form_state(db, form), "accepted": False, "reason": "表单已过期，请让 Agent 重新发一张"}

    merged = {**_answers(form), **{str(k): v for k, v in (values or {}).items()}}
    plan = _plan_for(db, form, merged)

    if plan.get("status") == "review" and confirm:
        fresh = str((plan.get("form") or {}).get("submit_value") or "")
        if plan_digest and plan_digest != fresh:
            # 页面上那份方案已经不是现在这份了：不拿旧确认放行，退回去重看。
            form.answers_json = json.dumps(merged, ensure_ascii=False, default=str)
            db.commit()
            return {
                **form_state(db, form),
                "accepted": False,
                "reason": "方案在你提交前发生了变化，请重新核对",
            }
        merged[str((plan.get("form") or {}).get("submit_key") or "__confirm_plan")] = fresh
        plan = _plan_for(db, form, merged)

    if plan.get("status") in {"ask", "review"}:
        # 参数没填齐，或审查还没被确认：表单原地继续，不落提交。
        form.answers_json = json.dumps(
            plan.get("answers") or merged, ensure_ascii=False, default=str
        )
        form.stage = "review" if plan["status"] == "review" else "decide"
        db.commit()
        return {
            **form_state(db, form),
            "accepted": False,
            "reason": "还有参数没定下来" if plan["status"] == "ask" else "请核对执行方案后确认",
        }

    form.submitted_json = json.dumps(
        plan.get("answers") or merged, ensure_ascii=False, default=str
    )
    form.answers_json = form.submitted_json
    form.status = "submitted"
    form.submitted_at = _now()
    db.commit()
    return {**form_state(db, form), "accepted": True}


def submitted_answers(db: Session, form: McpFlowForm) -> dict[str, Any]:
    return _answers(form)
