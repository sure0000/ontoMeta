"""交互式任务流程：把六环确认表单变成一问一答。

**为什么要有**：Web 里的 Data Agent 有 ``request_form``——弹一张带真实候选的表单让人点。
接在 dsh / Claude Code 这类纯文本客户端上的通用 Agent 没有这个出口，于是它要么把七八个
参数一次性问成一段话（人答不全），要么替用户猜一个 id（猜错也不报错，执行时才炸）。

这里把**同一张表单**拆成一次一个问题的流程：工具返回"现在该问什么 + 真实候选"，Agent
原样摆给用户，用户回一个选择，Agent 带着累计答案再调一次，直到 ``status="ready"`` 时
拿到可以照抄的 ``propose_*`` 参数。

三条设计约束：

1. **问题与候选与 Web 同源**。字段骨架取 ``ChatBiService.build_task_form``——同一个同步
   任务，在对话里问哪几项、候选是什么，与在 Web 表单里看到的逐字一致。这里另写一份"该问
   什么"就等于让两个入口对同一件事有两套事实。
2. **无服务端状态**。整条流程由 ``(kind, answers)`` 完全决定，每次重算。stdio、无状态
   HTTP、断线重连、换个会话接着问，行为都一样，也不需要一张会话表。
3. **不替用户选**。工具只把候选摆出来；只有"候选唯一"或"字段自带确定默认值"才自动采纳，
   且必须在这一环的确认步骤里原样展示出来让人核对（六环确认的文本版）。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from sqlalchemy.orm import Session

from . import AuthContext, ToolResult, register_tool
from ._common import session

_KINDS = ("sync", "transform", "materialize", "metric")

_KIND_LABELS = {
    "sync": "数据同步（把源库表搬进数仓 ODS）",
    "transform": "数据加工（读已同步的 ODS，产出清洗/转换结果表）",
    "materialize": "本体物化（把本体对象建成物理表，只出 DDL 不搬数）",
    "metric": "指标聚合（按已发布口径产出 ADS 结果表）",
}

#: 判"用户说的是哪类任务"的词。命中只用来**推荐**，最终仍由用户选——
#: 措辞与真实意图对不上是常态（"把客户表弄到数仓"四个类型都沾边）。
_KIND_MARKERS = {
    "sync": ("同步", "入仓", "落数", "搬数", "搬过来", "抽数", "ods", "cdc", "增量"),
    "transform": ("加工", "清洗", "转换", "宽表", "dwd", "dws", "维度表", "去重"),
    "materialize": ("物化", "建表", "建结构", "ddl", "落地结构"),
    "metric": ("指标", "口径", "聚合", "ads", "gmv", "kpi", "统计口径"),
}

#: 执行方案的确认位。以 ``__`` 开头的键都是流程自己的记账，不进 propose 的 context。
#: **整条流程只有这一次确认**：其余参数由本体、契约和默认值推导，在审查里一次核对。
#: （此前是按六环各确认一次，用户实测后的原话是"6 环确实太繁琐"。）
_PLAN_CONFIRM = "__confirm_plan"

#: 执行审查里要摆出来的 Spec 字段与它们的人话标签（按这个顺序展示，缺的跳过）。
#: 摆的是 **Drafter 派生的那份 Spec**，不是用户刚填的值——审查要审的正是"我填的东西
#: 到了执行期会变成什么"。
_SPEC_LABELS = {
    "object_display_name": "业务对象",
    "logic_name": "业务口径",
    "source": "来源",
    "sources": "来源",
    "target": "落点",
    "target_table": "目标表",
    "target_database": "目标库",
    "target_layer": "分层",
    "selected_targets": "物化范围",
    "mode": "装载方式",
    "load_strategy": "装载策略",
    "idempotency_strategy": "幂等策略",
    "primary_keys": "业务主键",
    "incremental_column": "增量字段",
    "initial_watermark": "初始水位",
    "sequence_column": "Sequence 列",
    "delete_policy": "DELETE 策略",
    "partition_key": "分区键",
    "cleansing_rules": "清洗规则",
    "refresh_cron": "调度",
    "engine": "执行引擎",
}
_FREE_TEXT_TYPES = {"text", "textarea", "number", "cron", "date"}
_MULTI_TYPES = {"multiselect"}
#: 闭集字段：取值必须来自候选（autocomplete 不在其中——它的候选是建议，不是闭集）。
_CHOICE_TYPES = {"select", "radio", "multiselect"}
#: 一次最多摆多少个候选。几百个对象全倒进回答里，人读不完，模型也会开始编。
_OPTION_LIMIT = 25


def _service():
    from app.services.chat_bi import ChatBiService

    return ChatBiService()


def build_proposal(db: Session, **kwargs: Any) -> dict[str, Any]:
    """跑一遍真 Drafter + 校验。局部导入：``app.agents`` 在导入期注册整条写侧流水线。"""
    from app.mcp.tools.proposals import build_proposal as _build

    return _build(db, **kwargs)


def _plan_digest(kind: str, context: dict[str, Any]) -> str:
    """把"确认"绑到**被审查的那一份方案**上。

    只认一句 "yes" 的话，用户在审查后改了一格装载方式，同一句确认照样放行——确认过的方案
    与执行的方案就不是同一份了。与 confirm/execute 的 ``host_confirmation`` 是同一套办法。
    """
    canonical = json.dumps(
        {"kind": kind, "context": context},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _clean_answers(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    return {str(k): v for k, v in raw.items() if v is not None and v != ""}


def _kind_options(goal: str) -> list[dict[str, Any]]:
    text = (goal or "").lower()
    scored = {
        kind: sum(1 for marker in markers if marker in text)
        for kind, markers in _KIND_MARKERS.items()
    }
    best = max(scored.values()) if scored else 0
    return [
        {
            "value": kind,
            "label": _KIND_LABELS[kind],
            **({"recommended": True} if best and scored[kind] == best else {}),
        }
        for kind in _KINDS
    ]


def _ontology_options(db: Session) -> list[dict[str, Any]]:
    from app.models.ontology import Ontology, OntologyStatus

    rows = (
        db.query(Ontology)
        .filter(Ontology.status.in_([OntologyStatus.PUBLISHED.value, OntologyStatus.DRAFT.value]))
        .all()
    )
    options = []
    for row in rows:
        # 一域一本体：本体行没有自己的名字，人认得的是数据域名（见 Ontology 的文档串）。
        domain = row.domain_context.name if row.domain_context else "未知数据域"
        published = row.status == OntologyStatus.PUBLISHED.value
        options.append(
            {
                "value": row.id,
                "label": domain,
                "detail": f"已发布 v{row.version}" if published else "草稿",
                **({"recommended": True} if published else {}),
            }
        )
    options.sort(key=lambda o: (not o.get("recommended"), o["label"]))
    return options


def _visible(field: dict, answers: dict) -> bool:
    """``visible_when`` 与 Web 表单同义：条件不满足的字段既不问、也不校验、也不提交。"""
    cond = field.get("visible_when")
    if not isinstance(cond, dict):
        return True
    current = answers.get(str(cond.get("field") or ""))
    allowed = cond.get("in")
    if isinstance(allowed, list):
        return current in allowed
    return current == cond.get("equals")


def _property_options(
    db: Session, ontology_id: str, object_type: str
) -> tuple[list[dict], list[str]]:
    """某个对象的字段候选（同步的主键 / 增量列 / sequence 列都必须是那张表上的列）。"""
    from app.api.ontology import list_ontology_properties

    rows = list_ontology_properties(ontology_id, object_type=object_type, db=db)
    options = [
        {
            "value": row.name,
            "label": (
                f"{row.display_name}（{row.name}）"
                if row.display_name and row.display_name != row.name
                else row.name
            ),
            **({"detail": row.data_type} if row.data_type else {}),
        }
        for row in rows
    ]
    return options, [row.name for row in rows if row.is_identity]


def _resolve_field(db: Session, field: dict, *, ontology_id: str, answers: dict) -> dict:
    """把字段的候选补全：联动候选（``options_by_value``）与实时候选（``options_from``）。

    Web 前端把这两件事做在浏览器里（选完对象再拉字段）。文本流程没有第二次渲染的机会，
    故在服务端就地解析——否则主键那一格会摆出一个空候选表，模型只能自己编列名。
    """
    resolved = dict(field)
    upstream = str(answers.get(str(field.get("depends_on") or "")) or "")
    if field.get("options_from") == "object_properties":
        if not upstream:
            resolved["options"] = []
            resolved["blocked_by"] = field.get("depends_on")
            return resolved
        options, identity = _property_options(db, ontology_id, upstream)
        resolved["options"] = options
        if identity and field["name"] == "primary_keys" and not resolved.get("default"):
            # 命中 <对象>_id / id 约定的字段预选成主键——与 Web 同一判据，只在有把握时给。
            resolved["default"] = identity
        return resolved
    if "options_by_value" in field:
        options_by_value = field["options_by_value"] or {}
        if not upstream or upstream not in options_by_value:
            # 上游还没选（或上游回答本身没命中候选）时，不能把依赖字段误报成“无候选”。
            # 先让调用方修正/选择 depends_on，下一轮再计算这里的真实候选。
            resolved["options"] = []
            resolved["blocked_by"] = field.get("depends_on")
        else:
            # key 存在但值为空，才表示这个已选上游确实没有下游候选，应当阻断。
            resolved["options"] = options_by_value.get(upstream) or []
    return resolved


def _match(field: dict, value: Any) -> Any | None:
    """把用户的回答对到真实候选上；对不上返回 None（宁可再问一遍，也不填一个假值）。"""
    from app.services.chat_bi_tool_schemas import _match_option

    options = field.get("options") or []
    if field.get("type") in _MULTI_TYPES:
        values = value if isinstance(value, list) else [value]
        # 逐项对候选时必须换成**单选**视图：拿多选字段本身去对单个值，会再次走进这一支，
        # 无限递归（materialize 的 selected_targets 一进来就炸）。
        single = {**field, "type": "select"}
        matched = [m for m in (_match(single, item) for item in values) if m is not None]
        return matched or None
    if isinstance(value, list):
        value = value[0] if value else None
        if value is None:
            return None
    text = str(value).strip()
    if not text:
        return None
    if not options and field.get("type") in _CHOICE_TYPES:
        # 闭集字段却一个候选都没取到（目录读失败、上游还没选）：这时**不能**放行，
        # 否则一个没人校验过的 id 会一路带到 Spec 里，执行时才炸。
        return None
    hit = _match_option({**field, "type": field.get("type") or "text"}, text)
    if hit is not None:
        return hit
    # 序号回答：用户说"3"，模型往往原样转发。仅在候选里没有同名值时才按序号解释，
    # 且序号对的是**摆出去的那一页**（截断后的顺序确定，重算一致）。
    if options and re.fullmatch(r"\d{1,3}", text):
        index = int(text)
        shown = options[:_OPTION_LIMIT]
        if 1 <= index <= len(shown):
            return shown[index - 1]["value"]
    return None


def _option_view(
    field: dict, *, search: str = "", limit: int = _OPTION_LIMIT
) -> tuple[list[dict], int]:
    options = [
        {
            "value": option.get("value"),
            "label": option.get("label") or option.get("value"),
            **({"detail": option["detail"]} if option.get("detail") else {}),
            **({"disabled": True} if option.get("disabled") else {}),
            **({"recommended": True} if option.get("recommended") else {}),
        }
        for option in (field.get("options") or [])
    ]
    if search:
        needle = search.strip().lower()
        options = [
            option
            for option in options
            if needle in str(option["label"]).lower() or needle in str(option["value"]).lower()
        ]
    return options[:limit], len(options)


def _label_of(field: dict, value: Any) -> str:
    options = {
        str(o.get("value")): str(o.get("label") or o.get("value"))
        for o in field.get("options") or []
    }
    if isinstance(value, list):
        return "、".join(options.get(str(item), str(item)) for item in value) or "—"
    if value in (None, "", []):
        return "—"
    return options.get(str(value), str(value))


def _field_view(
    field: dict,
    *,
    value: Any,
    auto: bool,
    error: str = "",
    search: str = "",
    option_limit: int = _OPTION_LIMIT,
) -> dict[str, Any]:
    """一个格子在表单里的样子：标签、控件类型、真实候选、当前值、是不是系统填的。"""
    options, total = _option_view(field, search=search, limit=option_limit)
    ftype = str(field.get("type") or "select")
    view: dict[str, Any] = {
        "key": field["name"],
        "label": field.get("label") or field["name"],
        "type": ftype,
        "multi": ftype in _MULTI_TYPES,
        "free_text": ftype in _FREE_TEXT_TYPES,
        "required": bool(field.get("required")),
        "value": value if value not in (None, "") else None,
        "value_label": _label_of(field, value),
        # auto=系统按默认值或唯一候选替用户填的。表单里要标出来，让人重点核对这几格。
        "auto": bool(auto),
        "options": options,
        "options_total": total,
        "options_truncated": total > len(options),
    }
    if field.get("help"):
        view["help"] = str(field["help"])
    if field.get("placeholder"):
        view["placeholder"] = str(field["placeholder"])
    if error:
        view["error"] = error
    if view["options_truncated"]:
        view["note"] = (
            f"共 {total} 个候选，只给了前 {len(options)} 个；"
            "用户直接说名称也能命中，或用 search 参数再筛。"
        )
    return view


_FORM_INSTRUCTION = (
    "把 form.fields **一次性做成一张表单**问用户，不要一格一格挤牙膏：\n"
    "- 宿主有交互问答工具（dsh 的 ask_user_question、Claude Code 的 AskUserQuestion 等）"
    "就用它，一次调用带上这一环的全部 fields，每格用 label 作问题、options 作候选"
    "（label 给人看、value 回给你），free_text=true 的格子让用户自由填；\n"
    "- 没有这类工具就退回一张编号清单，一条消息列完这一环所有格子，让用户一次回答。\n"
    "标了 auto=true 的是系统替用户填的，必须原样摆出来让人核对。\n"
    "用户填完：把每个 key 的取值写进 answers，并置 answers[\"{submit_key}\"]=\"yes\" "
    "表示这一环已确认，再调 advance_task_flow。不要替用户填，也不要跳过整张表直接确认。"
)


def _form_payload(
    *,
    kind: str | None,
    title: str,
    fields: list[dict],
    answers: dict,
    submit_key: str | None,
    ring: dict | None = None,
    note: str = "",
) -> dict[str, Any]:
    instruction = _FORM_INSTRUCTION.replace("{submit_key}", submit_key or "")
    if submit_key is None:
        instruction = (
            "把 form.fields 做成一张表单问用户（宿主有 ask_user_question / "
            "AskUserQuestion 就用它，没有就摆编号清单），把取值写进 answers 后再调"
            " advance_task_flow。这一步不属于六环确认，没有 submit_key。"
        )
    payload: dict[str, Any] = {
        "status": "ask",
        "flow": kind,
        "form": {
            "title": title,
            "submit_key": submit_key,
            "fields": fields,
        },
        "answers": answers,
        "instruction": instruction,
    }
    if ring:
        payload["ring"] = ring
        payload["form"]["note"] = (
            f"六环的第 {ring['index']}/{ring['total']} 环。"
            "一环一张表单，填完这一环再谈下一环，不要三环合成一张问完。"
        )
    if note:
        payload["form"]["note"] = note
    return payload


def _spec_summary(spec: dict[str, Any], fields: list[dict]) -> list[dict[str, str]]:
    """把 Spec 摘成人话表格。取值优先用表单候选里的 label（``full`` → 全量覆盖）。"""
    by_name = {f["name"]: f for f in fields}
    rows: list[dict[str, str]] = []
    for key, label in _SPEC_LABELS.items():
        if key not in spec:
            continue
        value = spec[key]
        if value in (None, "", [], {}):
            continue
        field = by_name.get(key)
        rows.append(
            {
                "key": key,
                "label": label,
                "value": _label_of(field, value) if field else _label_of({}, value),
            }
        )
    return rows


def _plan_notes(spec: dict[str, Any], validation: dict[str, Any]) -> list[str]:
    """执行审查里必须说清的几件事——都从 Spec 读，不猜。"""
    notes: list[str] = []
    mode = str(spec.get("mode") or spec.get("load_strategy") or "")
    if mode == "full":
        notes.append("全量覆盖：每次运行都会重写目标表的全部数据。")
    elif mode == "incremental":
        notes.append("增量：只搬增量字段大于上次水位的行，靠业务主键去重。")
    elif mode == "cdc":
        notes.append("CDC：常驻流作业，按 checkpoint 续跑。")
    cron = str(spec.get("refresh_cron") or "")
    notes.append(f"调度：{cron}" if cron else "没有配调度：只会手动触发，跑一次不等于建成管道。")
    blocking = int(validation.get("blocking_count") or 0)
    if blocking:
        notes.append(f"有 {blocking} 条阻断项，先修掉才能执行。")
    return notes


def _review_payload(
    *,
    kind: str,
    answers: dict,
    proposal: dict[str, Any],
    fields: list[dict],
    auto_filled: list[str],
    search: str,
    option_limit: int,
    digest: str,
    stale: bool = False,
) -> dict[str, Any]:
    """执行审查：整条流程**唯一**一次人工确认。

    摆三样东西：这次真会执行的方案（Drafter 派生的 Spec）、校验结果、以及可就地改的
    全部参数。改任何一格都会作废这次确认并重算方案——审查过的方案与执行的方案必须是同一份。
    """
    issues = [i for i in (proposal["validation"].get("issues") or []) if i.get("blocking")][:10]
    views = [
        _field_view(
            f,
            value=answers.get(f["name"]),
            auto=f["name"] in auto_filled,
            search=search,
            option_limit=option_limit,
        )
        for f in fields
    ]
    grouped: dict[str, list[dict]] = {}
    for view, field in zip(views, fields):
        grouped.setdefault(str(field.get("confirmation_node") or "data"), []).append(view["key"])
    return {
        "status": "review",
        "flow": kind,
        "answers": answers,
        "review": {
            "name": proposal["name"],
            "plan": _spec_summary(proposal["spec"], fields),
            "notes": _plan_notes(proposal["spec"], proposal["validation"]),
            "blocking_count": proposal["validation"]["blocking_count"],
            "blocking_issues": [
                {"code": i.get("code"), "message": i.get("message")} for i in issues
            ],
            "plan_digest": digest,
            **({"stale_confirmation": True} if stale else {}),
        },
        "form": {
            "title": "执行审查",
            "submit_key": _PLAN_CONFIRM,
            "submit_value": digest,
            "fields": views,
            "groups": grouped,
        },
        "instruction": (
            "先把 review.plan（这次真会执行的方案）和 review.notes 摆给用户看，再问他确认。"
            "要改哪一项就改 answers 里对应的键再调一次——改动会作废确认并重算方案。"
            f"他确认后把 answers[\"{_PLAN_CONFIRM}\"] 设成 form.submit_value（本次方案的 "
            "digest，原样抄；写 \"yes\" 不算数）。标了 auto=true 的是系统推导的，"
            "重点核对那几格。这是执行前唯一一次人工确认，不要替用户点头。"
            + ("\n上一次确认对应的是改动前的方案，已作废，请重新确认。" if stale else "")
        ),
    }


def _context(kind: str, fields: list[dict], answers: dict) -> dict[str, Any]:
    """把答案翻译成 propose_* 的 context。

    ``target_location`` 的候选值本身写成 ``键=值,键=值``（数据源与库在物化里是联动的一次
    选择），在这里拆回两个 context 键——拆错就会出现"选了 A 源 + B 源上的库"。
    """
    context: dict[str, Any] = {}
    # 只认这次表单真有的字段：answers 里可能还躺着 kind、流程记账，或换本体前的旧答案。
    names = {field["name"] for field in fields}
    for key, value in answers.items():
        if key.startswith("__") or key == "task_requirement":
            continue
        if key not in names and key != "ontology_id":
            continue
        if key == "target_location" and isinstance(value, str) and "=" in value:
            for chunk in value.split(","):
                if "=" in chunk:
                    sub_key, sub_value = chunk.split("=", 1)
                    context[sub_key.strip()] = sub_value.strip()
            continue
        context[key] = value
    return context


def _plan(
    db: Session,
    *,
    kind: str,
    answers: dict,
    goal: str,
    search: str = "",
    option_limit: int = _OPTION_LIMIT,
) -> dict:
    """整条流程的唯一判据：给定 (kind, answers) 算出"这一环的表单长什么样"。"""
    answers = dict(answers)

    if kind not in _KINDS:
        return _form_payload(
            kind=None,
            title="这是哪一类数据任务？",
            fields=[
                _field_view(
                    {
                        "name": "kind",
                        "label": "任务类型",
                        "type": "radio",
                        "required": True,
                        "options": _kind_options(goal),
                        "help": "选错类型后面问的东西就全错了；不确定就把用户的原话再问清楚一句。",
                    },
                    value=None,
                    auto=False,
                )
            ],
            answers=answers,
            submit_key=None,
        )

    ontology_id = str(answers.get("ontology_id") or "").strip()
    if not ontology_id:
        options = _ontology_options(db)
        if not options:
            return {
                "status": "blocked",
                "flow": kind,
                "answers": answers,
                "reason": "还没有可用的本体，先在本体建模里建一个并发布。",
            }
        if len(options) == 1:
            ontology_id = options[0]["value"]
            answers["ontology_id"] = ontology_id
        else:
            return _form_payload(
                kind=kind,
                title="在哪个本体上做这件事？",
                fields=[
                    _field_view(
                        {
                            "name": "ontology_id",
                            "label": "本体（数据域）",
                            "type": "select",
                            "required": True,
                            "options": options,
                        },
                        value=None,
                        auto=False,
                        search=search,
                    )
                ],
                answers=answers,
                submit_key=None,
            )

    intent = str(answers.get("task_requirement") or goal or "").strip()
    form = _service().build_task_form(
        db,
        kind=kind,
        ontology_id=ontology_id,
        title=(intent or _KIND_LABELS[kind])[:120],
        intent=intent,
    )
    raw_fields = form.get("fields") or []
    if not raw_fields:
        return {
            "status": "blocked",
            "flow": kind,
            "answers": answers,
            "reason": (
                f"这个本体上列不出 {kind} 任务的候选（可能没有符合条件的对象、口径或数据源）。"
                "先用 query_objects / search_logics / list_datasources 看看缺什么。"
            ),
        }

    # ``__auto`` 跟着 answers 走：哪些值是系统替用户填的，表单里要标出来让人核对。
    auto_filled: list[str] = list(answers.get("__auto") or [])
    errors: dict[str, str] = {}
    resolved_fields: list[dict] = []

    for field in raw_fields:
        resolved = _resolve_field(db, field, ontology_id=ontology_id, answers=answers)
        resolved_fields.append(resolved)
        name = resolved["name"]
        if not _visible(resolved, answers):
            # 隐藏字段不参与提交，留着会被 Drafter 读到（全量同步不该带着 CDC 的参数走）
            answers.pop(name, None)
            continue
        if name in answers:
            matched = _match(resolved, answers[name])
            if matched is None:
                # 对不上的值**留在表单里**并标错，不静默丢弃、更不拿默认值顶替——
                # 否则用户看到的是系统替他改过的那一格，还以为是自己填的。
                errors[name] = f"「{_label_of(resolved, answers[name])}」对不上候选，请重选"
                continue
            answers[name] = matched
            if name in auto_filled and matched != resolved.get("default"):
                auto_filled.remove(name)
            continue
        default = resolved.get("default")
        if default not in (None, "", []):
            answers[name] = default
            if name not in auto_filled:
                auto_filled.append(name)
    answers["__auto"] = auto_filled

    visible = [f for f in resolved_fields if _visible(f, answers)]

    # **只问定不下来的**。有默认值、唯一候选、可选项一律自动填，摆进最后那张执行审查里
    # 由人一次核对——按环逐张确认过一轮之后，用户的原话是"6 环确实太繁琐"。
    decisions = [
        f
        for f in visible
        if f.get("required")
        and (f["name"] in errors or f["name"] not in answers)
        # 依赖字段要等上游选择完成后再展开；否则“源数据源”会在“对象”之前
        # 以空 options 被误判为真正的无候选。
        and not (
            f.get("blocked_by")
            and (
                not answers.get(str(f.get("blocked_by")))
                or str(f.get("blocked_by")) in errors
            )
        )
    ]
    if decisions:
        blocked = [
            f
            for f in decisions
            if not (f.get("options") or [])
            and str(f.get("type") or "select") not in _FREE_TEXT_TYPES
        ]
        if blocked:
            names = "、".join(str(f.get("label") or f["name"]) for f in blocked)
            return {
                "status": "blocked",
                "flow": kind,
                "answers": answers,
                "reason": (
                    f"{names}没有可选候选，这个任务配不出来。"
                    + " ".join(str(f.get("help") or "") for f in blocked)
                ).strip(),
            }
        return _form_payload(
            kind=kind,
            title="还需要你定这几项",
            fields=[
                _field_view(
                    f,
                    value=answers.get(f["name"]),
                    auto=False,
                    error=errors.get(f["name"], ""),
                    search=search,
                    option_limit=option_limit,
                )
                for f in decisions
            ],
            answers=answers,
            submit_key=None,
            note=(
                "只列了系统定不下来的项；其余参数已按本体、契约和默认值推导好，"
                "会在最后的执行审查里一次给你核对。"
            ),
        )

    context = _context(kind, visible, answers)
    try:
        proposal = build_proposal(db, kind=kind, intent=intent or _KIND_LABELS[kind], context=context)
    except ValueError as exc:
        # Drafter 的拒绝是业务结论（「这个对象没有物理源表」之类），原样回给调用方。
        return {"status": "blocked", "flow": kind, "answers": answers, "reason": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"status": "blocked", "flow": kind, "answers": answers, "reason": f"生成方案失败：{exc}"}

    digest = _plan_digest(kind, context)
    given = str(answers.get(_PLAN_CONFIRM) or "").strip()
    if given != digest:
        return _review_payload(
            kind=kind,
            answers=answers,
            proposal=proposal,
            fields=visible,
            auto_filled=auto_filled,
            search=search,
            option_limit=option_limit,
            digest=digest,
            # 给了确认位但对不上 = 审查之后参数又改过（或抄成了 "yes"）：不能拿旧确认放行。
            stale=bool(given),
        )

    return {
        "status": "ready",
        "flow": kind,
        "answers": answers,
        "auto_filled": auto_filled,
        "proposal": {
            "name": proposal["name"],
            "plan_digest": digest,
            "spec_summary": _spec_summary(proposal["spec"], visible),
            "blocking_count": proposal["validation"]["blocking_count"],
        },
        "next_call": {"tool": "draft_task", "arguments": proposal["draft_payload"]},
        "then": [
            "validate_task（落库后再校验一次，有阻断项就回来改参数）",
            "confirm_task → execute_task（都要 publisher，且逐条由人放行）",
            "wait_task_status（等终态，别把 accepted 当成功）",
        ],
        "instruction": (
            "用户已确认执行方案。照抄 next_call 调 draft_task 落草稿，"
            "再按 ontometa-task-execute 走确认与执行；执行前的最后一次人工放行不能省。"
        ),
    }


_ANSWERS_SCHEMA = {
    "type": "object",
    "description": (
        "已经定下的答案，键是表单里各字段的 key，值用候选的 value（用户填的 label "
        "或名称也认，服务端会对回真实候选）。"
        "**每次都要把之前的答案原样带上**（这个流程不在服务端存状态）；"
        "用户一开始就说清楚的参数可以直接放进来，能对上候选就不会再问。"
        "提交某一环时同时置 __confirm_<环>=\"yes\"（环名见 form.submit_key）；"
        "想重新打开已确认的一环，把那个键删掉即可。"
    ),
}


def _console_url(db: Session, path: str) -> tuple[str, str]:
    """把表单路径拼成可点的链接。取设置里的控制台地址，没配就只给路径并说清怎么配——
    主机名不能推导：后端听什么地址与用户从哪访问它是两回事（见 DEVELOPMENT_PRINCIPLES）。"""
    from app.api.deps import settings_service

    try:
        base = str(settings_service.get_mcp_settings(db).get("mcp_console_base_url") or "").strip()
    except Exception:  # noqa: BLE001 - 读不到设置不该让发表单这件事失败
        base = ""
    if not base:
        return path, (
            "还没配 ontoMeta 控制台地址，只能给相对路径。"
            "让用户在 设置 → MCP 服务 填「控制台地址」后，链接才会是可直接点开的完整地址。"
        )
    return f"{base.rstrip('/')}{path}", ""


@register_tool
class OpenTaskFormTool:
    """把当前这一环变成控制台上的一张网页表单。"""

    name = "open_task_form"
    required_role = "editor"
    description = (
        "**客户端没有原生问答工具时用它**：把当前这一环变成 ontoMeta 控制台上的一张真表单，"
        "返回一个一次性链接发给用户；用户点开填完提交，你用 wait_task_form 取回填值继续。\n"
        "宿主有 ask_user_question / AskUserQuestion 时**不要用它**——直接在对话里渲染表单更快。\n"
        "入参与 advance_task_flow 相同（kind + 累计 answers）。链接有效期 2 小时。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": list(_KINDS), "description": "任务类型"},
            "answers": _ANSWERS_SCHEMA,
            "goal": {"type": "string", "description": "用户原话；用于需求预填"},
        },
        "required": ["kind", "answers"],
    }

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        from app.mcp.flow_forms import create_form

        answers = _clean_answers(arguments.get("answers"))
        kind = str(arguments.get("kind") or answers.get("kind") or "").strip()
        if kind not in _KINDS:
            return ToolResult(success=False, error=f"kind 必须是 {'、'.join(_KINDS)} 之一")
        goal = str(arguments.get("goal") or "").strip()
        try:
            with session() as db:
                plan = _plan(db, kind=kind, answers=answers, goal=goal)
                if plan.get("status") not in {"ask", "review"}:
                    # 没有要填、也没有要审的东西：原样把结论回给调用方。
                    return ToolResult(
                        success=True,
                        data=plan,
                        metadata={"status": plan["status"], "form_issued": False},
                    )
                stage = "review" if plan["status"] == "review" else "decide"
                if stage == "decide" and not plan["answers"].get("ontology_id"):
                    return ToolResult(
                        success=False,
                        error=(
                            "任务类型和本体这两步只有一个问题，直接问用户即可，不必发表单；"
                            "定下来后再调它。"
                        ),
                        data=plan,
                    )
                form = create_form(
                    db,
                    kind=kind,
                    stage=stage,
                    answers=plan["answers"],
                    goal=goal,
                    ontology_id=str(plan["answers"].get("ontology_id") or "") or None,
                    created_by=auth.principal_id,
                )
                url, hint = _console_url(db, f"/agent-access/task-form/{form.id}")
                expires = form.expires_at.isoformat() if form.expires_at else None
                title = plan["form"]["title"]
                field_count = len(plan["form"]["fields"])
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=f"生成表单失败：{exc}")
        return ToolResult(
            success=True,
            data={
                "form_id": form.id,
                "url": url,
                "stage": stage,
                "title": title,
                "field_count": field_count,
                "expires_at": expires,
                **({"hint": hint} if hint else {}),
                "instruction": (
                    "把链接给用户，让他点开填完提交；然后调 wait_task_form(form_id) 等回填。"
                    "不要替他填，也不要在他提交前继续往下走。"
                    + (
                        "这一步是**执行审查**：页面上会摆出真会执行的方案，他点确认才算数。"
                        if stage == "review"
                        else ""
                    )
                ),
            },
            metadata={"status": "form_issued", "stage": stage},
        )


@register_tool
class WaitTaskFormTool:
    """等用户把网页表单填完提交。"""

    name = "wait_task_form"
    required_role = "editor"
    description = (
        "等 open_task_form 发出去的那张表单被提交，再把填好的 answers 和下一环一起回给你。"
        "等待发生在服务端（最长 50 秒），不要用 Bash/sleep 或高频重复调用。\n"
        "超时返回 status=pending，如实告诉用户还没收到提交，再等一轮即可。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "form_id": {"type": "string", "description": "open_task_form 返回的 form_id"},
            "timeout_seconds": {
                "type": "number",
                "description": "服务端最长等待秒数（1-50，默认 50）",
                "default": 50,
                "minimum": 1,
                "maximum": 50,
            },
            "poll_interval_seconds": {
                "type": "number",
                "description": "服务端检查间隔秒数（1-15，默认 3）",
                "default": 3,
                "minimum": 1,
                "maximum": 15,
            },
        },
        "required": ["form_id"],
    }

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        import asyncio
        import time

        from app.mcp.flow_forms import get_form, submitted_answers

        form_id = str(arguments.get("form_id") or "").strip()
        if not form_id:
            return ToolResult(success=False, error="缺少 form_id")

        def _number(name: str, default: float, low: float, high: float) -> float:
            try:
                value = float(arguments.get(name, default))
            except (TypeError, ValueError):
                value = default
            return max(low, min(high, value))

        timeout = _number("timeout_seconds", 50, 1, 50)
        interval = _number("poll_interval_seconds", 3, 1, 15)
        started = time.monotonic()

        def _read() -> tuple[str, dict[str, Any] | None, dict[str, Any] | None]:
            # 每次重开会话：SQLite 上长活的会话读不到别的连接刚提交的行。
            with session() as db:
                form = get_form(db, form_id)
                if form is None:
                    return "missing", None, None
                if form.status != "submitted":
                    return form.status, None, None
                answers = submitted_answers(db, form)
                plan = _plan(db, kind=form.kind, answers=answers, goal=form.goal or "")
                return "submitted", answers, plan

        while True:
            try:
                status, answers, plan = _read()
            except Exception as exc:  # noqa: BLE001
                return ToolResult(success=False, error=f"读取表单失败：{exc}")
            if status == "missing":
                return ToolResult(success=False, error=f"表单不存在：{form_id}")
            if status == "submitted":
                return ToolResult(
                    success=True,
                    data={
                        "status": "submitted",
                        "answers": answers,
                        "next": plan,
                        "instruction": (
                            "用户已提交。next 就是下一环的表单（或 status=ready 的提案参数），"
                            "按 ontometa-flow 继续；answers 要原样带着往下传。"
                        ),
                    },
                    metadata={"status": "submitted", "waited_seconds": round(time.monotonic() - started, 3)},
                )
            if status == "expired":
                return ToolResult(
                    success=False,
                    error="表单已过期，重新调 open_task_form 发一张新的",
                )
            if time.monotonic() - started >= timeout:
                return ToolResult(
                    success=True,
                    data={
                        "status": "pending",
                        "timed_out": True,
                        "instruction": "还没收到提交。如实告诉用户表单还等着他填，再等一轮即可。",
                    },
                    metadata={"status": "pending", "waited_seconds": round(time.monotonic() - started, 3)},
                )
            await asyncio.sleep(min(interval, max(0.0, timeout - (time.monotonic() - started))))


@register_tool
class StartTaskFlowTool:
    """开一条交互式建数流程。"""

    name = "start_task_flow"
    # 与 propose_* 同级：它只读候选、不写库，但它是写侧的入口，reader 走到头也提不了案。
    required_role = "editor"
    description = (
        "用户想建数据任务（同步 / 加工 / 物化 / 指标）但参数没给全时，**先调它**。\n"
        "它按六环的节奏一次返回**一整环的表单**（form.fields：标签、控件类型、真实候选、"
        "系统预填了哪几格），你把这一环一次性做成表单问用户"
        "（宿主有 ask_user_question / AskUserQuestion 就用它，没有就摆编号清单），"
        "填完用 advance_task_flow 提交，直到 status=ready 时照抄 next_call 去 propose_*。\n"
        "问什么、候选是什么与 Web 表单同源，所以你不必自己想该问哪些参数，也不许自己编 id。\n"
        "已经知道类型或本体就一起给，能省掉对应的提问。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "goal": {
                "type": "string",
                "description": (
                    "用户的原话/目标，如「把客户主数据同步进数仓」。"
                    "用来推荐任务类型与预填需求。"
                ),
            },
            "kind": {
                "type": "string",
                "enum": list(_KINDS),
                "description": "已经确定的任务类型；不确定就别给，让用户选。",
            },
            "answers": _ANSWERS_SCHEMA,
        },
        "required": ["goal"],
    }

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        goal = str(arguments.get("goal") or "").strip()
        if not goal:
            return ToolResult(success=False, error="需要 goal（用户想做什么）")
        answers = _clean_answers(arguments.get("answers"))
        # 流程本身无服务端会话态：把原始需求放进累计答案，避免模型在选完 kind/本体
        # 后只回传局部答案，导致对象无法预选、依赖的源数据源被误判为空。
        if goal and not answers.get("task_requirement"):
            answers["task_requirement"] = goal
        # 第一张表单的字段 key 就是 "kind"，模型多半会把答案写进 answers 而不是提到参数上。
        # 两处都认，省掉一轮"参数放错地方"的返工。
        kind = str(arguments.get("kind") or answers.get("kind") or "").strip()
        try:
            with session() as db:
                plan = _plan(db, kind=kind, answers=answers, goal=goal)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=f"启动流程失败：{exc}")
        plan["goal"] = goal
        return ToolResult(success=True, data=plan, metadata={"status": plan["status"]})


@register_tool
class AdvanceTaskFlowTool:
    """带着用户填好的这一环推进流程。"""

    name = "advance_task_flow"
    required_role = "editor"
    description = (
        "把用户在表单里填的值写进 answers 后调它，拿下一环的表单；"
        "三环都确认完时返回 status=ready 和可以照抄的 propose_* 参数。\n"
        "answers 要**累计**（含 ontology_id 与各环的 __confirm_* 确认位），"
        "这个流程不存服务端状态；start_task_flow 会把原始 goal 放进 answers 的 task_requirement，"
        "后续调用仍要原样带上 answers。\n"
        "某一格的值对不上候选时，那一格会带着 error 回到表单里让用户重选，"
        "这一环的确认同时作废——不要绕过它自己挑一个值。\n"
        "status=blocked 说明缺前置条件，如实告诉用户，不要改用别的参数硬凑。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": list(_KINDS),
                "description": "任务类型（上一步返回的 flow）",
            },
            "answers": _ANSWERS_SCHEMA,
            "goal": {"type": "string", "description": "用户原话；用于需求预填，建议原样带着"},
            "search": {
                "type": "string",
                "description": "候选太多时按关键词筛这一环的候选（只影响这一次返回）",
            },
        },
        "required": ["kind", "answers"],
    }

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        answers = _clean_answers(arguments.get("answers"))
        kind = str(arguments.get("kind") or answers.get("kind") or "").strip()
        if kind not in _KINDS:
            return ToolResult(
                success=False,
                error=(
                    f"kind 必须是 {'、'.join(_KINDS)} 之一；"
                    "不确定就先用 start_task_flow 让用户选"
                ),
            )
        goal = str(arguments.get("goal") or "").strip()
        search = str(arguments.get("search") or "").strip()
        try:
            with session() as db:
                plan = _plan(db, kind=kind, answers=answers, goal=goal, search=search)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=f"推进流程失败：{exc}")
        if goal:
            plan["goal"] = goal
        return ToolResult(success=True, data=plan, metadata={"status": plan["status"]})
