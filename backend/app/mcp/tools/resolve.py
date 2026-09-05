"""把用户说的词解析成真实主体（对象 / 口径）。

**这个工具补的是一条纯粹的效率断路**，不是新能力。真机验收里，模型为了把「客户」和
「销售订单」两个中文词变成 id，一次会话开了 6 次 ``query_objects`` + 3 次
``query_object_detail``——13 次调用里 9 次是在找 id；全部 72 次工具调用里，带 ``search``
的 ``query_objects`` 是被调用最多的形态。原因是没有一个"给词、还我主体"的入口，
而 ``query_objects`` 的搜索既不把精确匹配置顶，也不告诉调用方"这个词在别的数据域里
还有一个同名的"。

所以这里一次给全模型下一步真正要的四件事：**是哪个（id）、在哪个本体、是什么角色、
能不能取数（落点）**。精确匹配置顶，同名跨域一律并列摆出来。

与 ``get_landing`` 的分工：那个回答"这个主体落到哪张表、什么状态"（单主体、事实全）；
这个回答"你说的这个词是哪个主体"（多候选、每个只给判别所需的最小面）。
"""

from __future__ import annotations

from typing import Any

from app.models import BusinessLogic, EntityStatus, ObjectType
from app.services.logic_query import OntologyQueryService
from app.services.object_landing import bulk_logic_landings, bulk_object_landings

from . import AuthContext, ToolResult, register_tool
from ._common import as_int, session

_query = OntologyQueryService()

_KINDS = ("object", "logic", "any")

# 排序**必须在截断之前**做，否则"精确命中置顶"只是把已取回那一页重排一遍：
# `公司` 在真库上命中 42 条，只取回 limit+1 条的话，erpnext 那个精确同名的
# 压根不在里面，`exact_count` 会错报成 1 —— 这正是 get_landing 栽过的那个坑。
# 所以先取一个宽窗口再排。窗口本身也可能被打满，届时如实说明。
_CANDIDATE_WINDOW = 200


def _exactness(item: dict[str, Any], needle: str) -> int:
    name = str(item.get("name") or "").strip().casefold()
    display = str(item.get("display_name") or "").strip().casefold()
    if needle in (name, display):
        return 0
    if name.startswith(needle) or display.startswith(needle):
        return 1
    return 2


def _object_row(item) -> dict[str, Any]:
    return {
        "kind": "object",
        "id": item.id,
        "name": item.name,
        "display_name": item.display_name,
        # ontology_id 不在读模型 ObjectTypeSummary 上，getattr 会静默给 None——
        # 而调用方下一步几乎一定要拿它去调别的工具。展示行确定后单独回填（见 _fill_ontology_ids）。
        "ontology_id": None,
        "domain_name": getattr(item, "domain_name", None),
        "table_role": getattr(item, "table_role", None),
        "status": getattr(item, "status", None),
        "needs_review": getattr(item, "needs_review", None),
        "landing": None,
    }


def _logic_row(item) -> dict[str, Any]:
    return {
        "kind": "logic",
        "id": item.id,
        "name": item.name,
        "display_name": item.display_name,
        "ontology_id": None,
        "domain_name": getattr(item, "domain_name", None),
        "logic_type": getattr(item, "logic_type", None),
        "formalized": getattr(item, "formalized", None),
        "status": getattr(item, "status", None),
        "landing": None,
    }


def _fill_ontology_ids(db, rows: list[dict[str, Any]]) -> None:
    """给展示行回填 ontology_id（读模型里没有这个字段）。只查展示的那几条。"""
    for model, kind in ((ObjectType, "object"), (BusinessLogic, "logic")):
        ids = [r["id"] for r in rows if r["kind"] == kind]
        if not ids:
            continue
        mapping = dict(
            db.query(model.id, model.ontology_id).filter(model.id.in_(ids)).all()
        )
        for row in rows:
            if row["kind"] == kind:
                row["ontology_id"] = mapping.get(row["id"])


def _fill_landings(db, rows: list[dict[str, Any]]) -> None:
    """落点只给"能不能取数"这一层；状态明细去 get_landing。只查展示的那几条。"""
    object_ids = [r["id"] for r in rows if r["kind"] == "object"]
    logic_ids = [r["id"] for r in rows if r["kind"] == "logic"]
    objects = bulk_object_landings(db, object_ids) if object_ids else {}
    logics = bulk_logic_landings(db, logic_ids) if logic_ids else {}
    for row in rows:
        landing = (objects if row["kind"] == "object" else logics).get(row["id"])
        if landing is None:
            continue
        picked = {"state": landing.state, "queryable": landing.queryable}
        if row["kind"] == "object":
            picked["ods_table"] = landing.ods_table
        picked["serving_table"] = landing.serving_table
        row["landing"] = picked


@register_tool
class ResolveSubjectTool:
    """词 → 真实主体"""

    name = "resolve_subject"
    required_role = "reader"
    description = (
        "把用户说的词（「客户」「销售订单」「公司」）解析成真实主体，一次给全下一步要的信息："
        "**id、所属本体与数据域、角色、发布状态、有没有物理落点/能不能取数**。\n"
        "**任何需要 id 的操作之前先调它**，不要用 query_objects 反复试探——"
        "那个搜索不把精确匹配置顶，也不会告诉你同名主体在别的数据域里还有一个。\n"
        "精确命中排在最前；`exact_count>1` 表示**同名跨域**（odoo 与 erpnext 各有一个「公司」，"
        "落点完全不同），此时必须按 `domain_name` 挑或带上 ontology_id 再问一次，不许取第一个。\n"
        "`landing=null` 就是没有落点登记——该主体查不了数，不要按命名规则推表名。\n"
        "只解析，不读数据；落点明细用 get_landing，字段与关系用 query_object_detail。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "keyword": {
                "type": "string",
                "description": "用户说的词：显示名或标识名，中英文皆可",
            },
            "kind": {
                "type": "string",
                "description": "只找业务对象、只找业务口径，还是两者都找",
                "enum": list(_KINDS),
                "default": "object",
            },
            "ontology_id": {
                "type": "string",
                "description": "限定本体；留空则跨本体检索（同名歧义靠返回的 domain_name 分辨）",
            },
            "published_only": {
                "type": "boolean",
                "description": (
                    "只看已发布主体。默认 false——未发布主体照样有落点，"
                    "过滤掉会让同名歧义看起来不存在"
                ),
                "default": False,
            },
            "limit": {
                "type": "integer",
                "description": "返回候选数上限",
                "default": 10,
                "minimum": 1,
                "maximum": 50,
            },
        },
        "required": ["keyword"],
    }

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        keyword = str(arguments.get("keyword") or "").strip()
        if not keyword:
            return ToolResult(success=False, error="缺少 keyword")
        kind = str(arguments.get("kind") or "object").strip().lower() or "object"
        if kind not in _KINDS:
            return ToolResult(
                success=False, error=f"kind 须为 {'/'.join(_KINDS)}，收到「{kind}」"
            )
        ontology_id = str(arguments.get("ontology_id") or "").strip() or None
        # 默认 false 是刻意的：发布状态一过滤，跨域同名就看起来"唯一"了——
        # get_landing 正是栽在这上面（见 tools/ops.py 的 keyword 解析）。
        published_only = bool(arguments.get("published_only", False))
        limit = as_int(arguments.get("limit"), 10, low=1, high=50)
        needle = keyword.casefold()

        try:
            with session() as db:
                rows: list[dict[str, Any]] = []
                object_total = logic_total = 0
                if kind in ("object", "any"):
                    page = _query.list_object_types(
                        db,
                        ontology_id=ontology_id,
                        published_only=published_only,
                        q=keyword,
                        limit=_CANDIDATE_WINDOW,
                    )
                    rows += [_object_row(item) for item in page.items]
                    object_total = page.total
                if kind in ("logic", "any"):
                    logic_page = _query.list_business_logics(
                        db,
                        ontology_id=ontology_id,
                        published_only=published_only,
                        q=keyword,
                        limit=_CANDIDATE_WINDOW,
                    )
                    rows += [_logic_row(item) for item in logic_page.items]
                    logic_total = logic_page.total

                rows.sort(
                    key=lambda item: (
                        _exactness(item, needle),
                        # 同等精确度下已发布优先，但绝不因此把未发布的藏起来。
                        0 if item.get("status") == EntityStatus.PUBLISHED.value else 1,
                        str(item.get("display_name") or item.get("name") or ""),
                    )
                )
                exact = [item for item in rows if _exactness(item, needle) == 0]
                shown = rows[:limit]
                # 只给展示行补 ontology_id 与落点：宽窗口是为了排序正确，不是为了多查库。
                _fill_ontology_ids(db, shown)
                _fill_landings(db, shown)

            total = object_total + logic_total
            window_full = len(rows) >= _CANDIDATE_WINDOW
            note = None
            if not rows:
                note = "没有匹配的主体。换个说法，或用 query_ontology 看有哪些本体。"
            elif len(exact) > 1:
                domains = sorted({str(item.get("domain_name") or "?") for item in exact})
                note = (
                    f"「{keyword}」精确命中 {len(exact)} 个主体"
                    + (f"，分属数据域：{'、'.join(domains)}" if len(domains) > 1 else "")
                    + "。按 domain_name 挑，或带 ontology_id 再问一次；不要取第一个。"
                )
            elif len(exact) == 1:
                note = "已精确命中唯一主体（exact_match）。"
            else:
                note = (
                    f"没有与「{keyword}」完全同名的主体，以下是部分匹配，"
                    "确认后再用它的 id。"
                )
            if window_full:
                note = (
                    f"{note} 候选过多（已按前 {_CANDIDATE_WINDOW} 条排序），"
                    "exact_count 可能不全；请带 ontology_id 收窄。"
                )
            return ToolResult(
                success=True,
                data={
                    "keyword": keyword,
                    "matches": shown,
                    # 唯一精确命中时直接把它拎出来，省调用方再判一次。
                    "exact_match": exact[0] if len(exact) == 1 else None,
                    "note": note,
                },
                metadata={
                    "count": len(shown),
                    "total": total,
                    "truncated": total > len(shown),
                    "exact_count": len(exact),
                    "exact_count_complete": not window_full,
                    "kind": kind,
                    "ontology_id": ontology_id,
                    "published_only": published_only,
                },
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=f"解析主体失败：{exc}")
