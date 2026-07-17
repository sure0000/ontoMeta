"""业务逻辑表达式格式化服务。

把用户在富文本编辑器里组合的 ``expression_draft``(自然语言文本段 + 标记引用段)
格式化为统一的 AST 风格 ``expression_json``。复用 ``LogicImportService`` 的
LLM/Mock 双模式构造范式:优先使用 SettingsService 配置的默认 LLM,缺失时走 mock
启发式,不阻塞保存。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from openai import OpenAI
from sqlalchemy.orm import Session

from app.config import settings
from app.models import DomainContext, ObjectType, Property
from app.services.query import OntologyQueryService

logger = logging.getLogger(__name__)


_METRIC_OPS = ("sum", "count", "avg", "min", "max", "distinct_count")
_AGG_RE = re.compile(r"\b(sum|count|avg|min|max)\s*\(", re.IGNORECASE)
_WHERE_RE = re.compile(
    r"\b(where|其中|当|若|如果|条件)\b",
    re.IGNORECASE,
)
_GROUP_RE = re.compile(
    r"\b(group\s*by|按|分组|维度)\b",
    re.IGNORECASE,
)


def _parse_draft(expression_draft: dict | None) -> tuple[list[dict], list[dict]]:
    """从 draft 中提取 segments 与去重后的 refs 列表。"""
    if not expression_draft or not isinstance(expression_draft, dict):
        return [], []
    raw_segments = expression_draft.get("segments") or []
    if not isinstance(raw_segments, list):
        return [], []
    segments: list[dict] = []
    refs: list[dict] = []
    seen: set[str] = set()
    for seg in raw_segments:
        if not isinstance(seg, dict):
            continue
        seg_type = seg.get("type")
        if seg_type == "text":
            value = seg.get("value") or ""
            if value:
                segments.append({"type": "text", "value": value})
        elif seg_type == "ref":
            ref_id = seg.get("ref_id") or f"r{len(refs) + 1}"
            ref = {
                "ref_id": ref_id,
                "object_type_id": seg.get("object_type_id"),
                "object_name": seg.get("object_name"),
                "object_display_name": seg.get("object_display_name"),
                "property_id": seg.get("property_id"),
                "property_name": seg.get("property_name"),
                "property_display_name": seg.get("property_display_name"),
            }
            if ref_id not in seen:
                refs.append(ref)
                seen.add(ref_id)
            segments.append({"type": "ref", "ref_id": ref_id})
    return segments, refs


def _segments_to_text(segments: list[dict], refs: list[dict]) -> str:
    """把 segments 渲染为带 ``[[ref:rN|对象.属性]]`` 占位的自然语言文本。"""
    ref_map = {r["ref_id"]: r for r in refs}
    parts: list[str] = []
    for seg in segments:
        if seg["type"] == "text":
            parts.append(seg["value"])
        else:
            ref = ref_map.get(seg["ref_id"])
            if not ref:
                parts.append(f"[[ref:{seg['ref_id']}]]")
                continue
            label_parts = [ref.get("object_display_name") or ref.get("object_name") or "?"]
            if ref.get("property_display_name") or ref.get("property_name"):
                label_parts.append(ref.get("property_display_name") or ref.get("property_name"))
            label = ".".join(label_parts)
            parts.append(f"[[ref:{seg['ref_id']}|{label}]]")
    return "".join(parts)


def _segments_to_summary(segments: list[dict], refs: list[dict]) -> str:
    """从 segments 派生纯文本摘要(供列表页/降级显示)。"""
    ref_map = {r["ref_id"]: r for r in refs}
    parts: list[str] = []
    for seg in segments:
        if seg["type"] == "text":
            parts.append(seg["value"])
        else:
            ref = ref_map.get(seg["ref_id"])
            if not ref:
                parts.append("@?")
                continue
            label_parts = [ref.get("object_display_name") or ref.get("object_name") or "?"]
            if ref.get("property_display_name") or ref.get("property_name"):
                label_parts.append(ref.get("property_display_name") or ref.get("property_name"))
            parts.append("@" + ".".join(label_parts))
    return "".join(parts).strip()


def _resolve_refs(db: Session, ontology_id: str, refs: list[dict]) -> list[dict]:
    """补全/校验 refs 的对象与字段元数据,确保归属已发布本体。"""
    if not refs:
        return []
    obj_ids = {r["object_type_id"] for r in refs if r.get("object_type_id")}
    prop_ids = {r["property_id"] for r in refs if r.get("property_id")}
    published_ontology_ids = set(OntologyQueryService()._published_ontology_ids(db))
    obj_map: dict[str, ObjectType] = {}
    prop_map: dict[str, Property] = {}
    if obj_ids:
        for obj in (
            db.query(ObjectType)
            .filter(ObjectType.id.in_(list(obj_ids)))
            .all()
        ):
            if obj.ontology_id in published_ontology_ids:
                obj_map[obj.id] = obj
    if prop_ids:
        for prop in (
            db.query(Property).filter(Property.id.in_(list(prop_ids))).all()
        ):
            obj = obj_map.get(prop.object_type_id) or db.get(ObjectType, prop.object_type_id)
            if obj and obj.ontology_id in published_ontology_ids:
                prop_map[prop.id] = prop
    resolved: list[dict] = []
    for r in refs:
        obj = obj_map.get(r.get("object_type_id"))
        prop = prop_map.get(r.get("property_id"))
        resolved.append(
            {
                "ref_id": r["ref_id"],
                "object_type_id": obj.id if obj else None,
                "object_name": obj.name if obj else r.get("object_name"),
                "object_display_name": obj.display_name if obj else r.get("object_display_name"),
                "property_id": prop.id if prop else None,
                "property_name": prop.name if prop else r.get("property_name"),
                "property_display_name": (
                    prop.display_name if prop else r.get("property_display_name")
                ),
            }
        )
    return resolved


def _ref_expr(ref_id: str) -> dict:
    return {"ref": ref_id}


def _literal_expr(value: Any) -> dict:
    return {"value": value}


def _mock_format(
    segments: list[dict],
    refs: list[dict],
    logic_type: str | None,
    description: str | None,
) -> dict:
    """规则启发式:从自然语言文本里识别聚合/过滤/分组,生成简化 AST。"""
    text = _segments_to_text(segments, refs)
    logic_type = (logic_type or "metric").lower()
    if logic_type not in {"metric", "tag", "rule"}:
        logic_type = "metric"

    # 收集出现在文本里的引用 id,按出现顺序
    used_ref_ids: list[str] = []
    for seg in segments:
        if seg["type"] == "ref" and seg["ref_id"] not in used_ref_ids:
            used_ref_ids.append(seg["ref_id"])

    if logic_type == "metric":
        # 0. 提前从文本中抽取过滤条件,用于后续判断哪些 ref 不应放入 args
        extracted_conds = _extract_conditions_from_text(segments, refs)
        filter_ref_ids = {c.get("left", {}).get("ref") for c in extracted_conds}

        # 1. 从文本中定位分组关键词,取其后第一个 ref 作为 group_by 维度
        group_by: list[dict] = []
        group_match = _GROUP_RE.search(text)
        if group_match and len(used_ref_ids) >= 2:
            suffix = text[group_match.end():]
            ref_m = _REF_PLACEHOLDER_RE.search(suffix)
            if ref_m and ref_m.group(1) in set(used_ref_ids):
                group_by = [_ref_expr(ref_m.group(1))]

        # 2. args 取第一个不在 group_by 且不在 filter 条件中的 ref
        group_ids = {g["ref"] for g in group_by}
        args_ref = next(
            (rid for rid in used_ref_ids
             if rid not in group_ids and rid not in filter_ref_ids),
            None,
        )
        args = [_ref_expr(args_ref)] if args_ref else []

        # 3. 聚合操作:无 args 时默认 count(统计满足条件的行数)
        op_match = _AGG_RE.search(text)
        if op_match:
            op = op_match.group(1).lower()
        else:
            op = "count" if not args else "sum"
        if op not in _METRIC_OPS:
            op = "sum"

        body = {
            "operation": op,
            "args": args,
            "filter": None,
            "group_by": group_by,
            "window": None,
        }
        body = _inject_heuristic_filter(body, segments, refs)
    elif logic_type == "tag":
        cases = []
        extracted = _extract_conditions_from_text(segments, refs)
        for c in extracted:
            cases.append({"when": c, "then": _literal_expr(None)})
        if not cases and used_ref_ids:
            cases.append(
                {
                    "when": {
                        "left": _ref_expr(used_ref_ids[0]),
                        "op": ">",
                        "right": _literal_expr(0),
                    },
                    "then": _literal_expr(None),
                }
            )
        cases.append({"when": None, "then": _literal_expr(None)})
        body = {"cases": cases}
    else:  # rule
        extracted = _extract_conditions_from_text(segments, refs)
        if extracted:
            cond = extracted[0]
        elif used_ref_ids:
            cond = {
                "left": _ref_expr(used_ref_ids[0]),
                "op": "=",
                "right": _literal_expr(None),
            }
        else:
            cond = None
        body = {"condition": cond, "message": description or "规则不满足"}

    return {
        "type": logic_type,
        "description": description or "",
        "refs": [_ref_meta(r) for r in refs],
        "body": body,
    }


def _ref_meta(r: dict) -> dict:
    """构造 refs 数组中单条引用的标识 + 语义字段。"""
    return {
        "ref_id": r["ref_id"],
        "object_type_id": r.get("object_type_id"),
        "object_name": r.get("object_name"),
        "object_display_name": r.get("object_display_name"),
        "property_id": r.get("property_id"),
        "property_name": r.get("property_name"),
        "property_display_name": r.get("property_display_name"),
    }


def _collect_ref_ids(node: Any, acc: set[str] | None = None) -> set[str]:
    """递归收集 AST 节点中所有 `{"ref": "rN"}` 出现的 ref_id。"""
    if acc is None:
        acc = set()
    if isinstance(node, dict):
        ref = node.get("ref")
        if isinstance(ref, str):
            acc.add(ref)
        for v in node.values():
            _collect_ref_ids(v, acc)
    elif isinstance(node, list):
        for item in node:
            _collect_ref_ids(item, acc)
    return acc


def _strip_unknown_refs(node: Any, valid_ids: set[str]) -> Any:
    """把 body 中引用了不存在 ref_id 的 `{"ref": "rN"}` 节点替换为字面量占位,
    避免下游解析报错。"""
    if isinstance(node, dict):
        ref = node.get("ref")
        if isinstance(ref, str) and ref not in valid_ids:
            return {"value": None}
        return {k: _strip_unknown_refs(v, valid_ids) for k, v in node.items()}
    if isinstance(node, list):
        return [_strip_unknown_refs(item, valid_ids) for item in node]
    return node


def _filter_has_valid_ref(filter_node: Any, valid_ids: set[str]) -> bool:
    """判断 filter 是否包含至少一个有效 ref 引用。
    用于在 strip 之后检测 filter 是否还有意义;无有效 ref 的 filter 视为无效。"""
    if filter_node is None:
        return False
    ids = _collect_ref_ids(filter_node)
    if not ids:
        # filter 里完全没有 ref(可能全是字面量或被 strip 掉),无效
        return False
    return bool(ids & valid_ids)


def _collect_null_value_leaf_refs(node: Any, acc: set[str] | None = None) -> set[str]:
    """收集 filter 条件树中所有右值为 null 的叶子条件的左值 ref_id。
    用于判断 LLM 产出的 filter 是否需要在归一化阶段用启发式填补。"""
    if acc is None:
        acc = set()
    if not isinstance(node, dict):
        return acc
    if "left" in node and "op" in node:
        right = node.get("right") or {}
        left_ref = node.get("left", {}).get("ref")
        if right.get("value") is None and isinstance(left_ref, str):
            acc.add(left_ref)
        return acc
    if node.get("op") in ("and", "or"):
        for c in node.get("conditions") or []:
            _collect_null_value_leaf_refs(c, acc)
    return acc


def _fill_null_filter_values(node: Any, by_ref: dict[str, dict]) -> Any:
    """遍历 filter 条件树,把右值为 null 的叶子条件替换为启发式抽取的结果(按 ref 匹配)。"""
    if not isinstance(node, dict):
        return node
    if "left" in node and "op" in node:
        ref = node.get("left", {}).get("ref")
        right = node.get("right") or {}
        if right.get("value") is None and isinstance(ref, str) and ref in by_ref:
            hc = by_ref[ref]
            node["right"] = hc.get("right", {"value": None})
            node["op"] = hc["op"]
        return node
    if node.get("op") in ("and", "or") and "conditions" in node:
        node["conditions"] = [
            _fill_null_filter_values(c, by_ref) for c in node["conditions"]
        ]
    return node


def _enhance_filter_null_values(body: dict, segments: list[dict], refs: list[dict]) -> dict:
    """LLM 产出 filter 中若存在右值为 null 的叶子条件,用启发式抽取填补。

    仅填补左值 ref 匹配且值不为 null 的条件;不影响 LLM 已正确识别值的条件。
    """
    filter_node = body.get("filter")
    if not filter_node:
        return body
    null_refs = _collect_null_value_leaf_refs(filter_node)
    if not null_refs:
        return body
    # 排除已在 args / group_by 中的 ref,避免把度量/维度字段错当过滤条件
    args_ids: set[str] = set()
    for a in body.get("args") or []:
        if isinstance(a, dict) and isinstance(a.get("ref"), str):
            args_ids.add(a["ref"])
    group_by_ids: set[str] = set()
    for a in body.get("group_by") or []:
        if isinstance(a, dict) and isinstance(a.get("ref"), str):
            group_by_ids.add(a["ref"])
    exclude_ids = args_ids | group_by_ids
    extracted = [
        c for c in _extract_conditions_from_text(segments, refs)
        if c.get("left", {}).get("ref") not in exclude_ids
    ]
    if not extracted:
        return body
    by_ref = {c["left"]["ref"]: c for c in extracted}
    body["filter"] = _fill_null_filter_values(filter_node, by_ref)
    return body


def _rewrite_condition_keys(node: Any) -> Any:
    """统一组合条件字段名为 `conditions`:把 LLM 误写的 `items` / `clauses` /
    `operands` 改名,且仅当节点是 and/or 组合条件时执行。"""
    if isinstance(node, dict):
        op = node.get("op")
        if op in ("and", "or") and ("items" in node or "clauses" in node or "operands" in node):
            items = node.get("items") or node.get("clauses") or node.get("operands") or []
            node = {k: v for k, v in node.items() if k not in ("items", "clauses", "operands")}
            node["conditions"] = items
        return {k: _rewrite_condition_keys(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_rewrite_condition_keys(item) for item in node]
    return node


# LLM 常见把 filter 写成的同义字段(按优先级排序)
_METRIC_FILTER_ALIASES = (
    "where",
    "having",
    "predicate",
    "filter_condition",
    "filter_cond",
    "conditions",
    "filter_conditions",
)


def _unify_metric_filter(body: dict) -> dict:
    """metric body 里把 where/having/predicate 等同义字段统一搬到 `filter`。"""
    if not isinstance(body, dict):
        return body
    if body.get("filter") is not None:
        # 已有 filter,但可能是错误形态(如裸列表),包成 {op:and, conditions:[...]}
        body["filter"] = _coerce_condition(body["filter"])
        return body
    for key in _METRIC_FILTER_ALIASES:
        val = body.get(key)
        if val is None:
            continue
        cond = _coerce_condition(val)
        if cond is not None:
            body["filter"] = cond
            # 删除同义字段,避免重复
            for k in _METRIC_FILTER_ALIASES:
                body.pop(k, None)
            return body
    return body


def _coerce_condition(val: Any) -> dict | None:
    """把任意形态(单条件 dict / 条件列表 / and-or dict)规整为合法 condition。"""
    if val is None:
        return None
    if isinstance(val, list):
        # 裸条件列表 → and(conditions)
        conds = [c for c in (_coerce_condition(v) for v in val) if c is not None]
        if not conds:
            return None
        if len(conds) == 1:
            return conds[0]
        return {"op": "and", "conditions": conds}
    if isinstance(val, dict):
        # 已经是 and/or 组合
        if val.get("op") in ("and", "or"):
            return _rewrite_condition_keys(val)
        # 已经是单比较条件
        if "left" in val and "op" in val:
            return _rewrite_condition_keys(val)
        # 嵌套在 condition/when 等键里
        for inner_key in ("condition", "where", "filter", "when"):
            inner = val.get(inner_key)
            if inner is not None:
                return _coerce_condition(inner)
    return None


# 过滤关键词(伪代码 + 自然语言),用于启发式识别 filter
_FILTER_KEYWORD_RE = re.compile(
    r"\b(where|having)\b|"
    r"(其中|当|若|如果|条件是|只统计|仅统计|排除|过滤|筛选|限定|且要求|并且)",
    re.IGNORECASE,
)

# 比较运算符(中文/英文/符号),按"长前缀优先"排序避免 `>` 吃掉 `>=`
_OP_ALTERNATIVES = [
    (">=", r"大于等于|不低于|不少于|至少|≥|>="),
    ("<=", r"小于等于|不高于|不超过|至多|最多|≤|<="),
    ("!=", r"不等于|不为|不是|非|≠|!=|<>"),
    (">", r"大于|超过|晚于|高于|>"),
    ("<", r"小于|低于|早于|<"),
    ("=", r"等于|为|是|:=|：|:|=|=="),
    ("like", r"包含|LIKE"),
    ("in", r"属于|IN|in"),
]
_OP_RE = re.compile("|".join(f"(?P<g{i}>{alt})" for i, (_, alt) in enumerate(_OP_ALTERNATIVES)))
_OP_ANY_RE = re.compile("|".join(alt for _, alt in _OP_ALTERNATIVES))

# 日期片段(容忍数字与年/月/日之间的空格)
_DATE_CN = r"\d{4}\s*年\s*\d{1,2}\s*月(?:\s*\d{1,2}\s*日)?"
_DATE_ISO = r"\d{4}[-/]\d{1,2}(?:[-/]\d{1,2})?"

# 值片段(单条):引号字符串 / 中文日期 / ISO 日期 / 数值 / 裸词
_VALUE_ANY_RE = re.compile(
    r"'[^']*'|\"[^\"]*\"|「[^」]*」|"
    r"(?:" + _DATE_CN + r")|"
    r"(?:" + _DATE_ISO + r")|"
    r"-?\d+(?:\.\d+)?|"
    r"[^\s,，。;；)）\]\【]+"
)

# ref 占位 + 紧跟 op + 值 的整体模式(ref 与 op 之间仅允许空白)
_REF_OP_VALUE_RE = re.compile(
    r"\[\[ref:(?P<rid>r\d+)(?:\|[^]]+)?\]\]"
    r"\s*"
    r"(?P<op>" + "|".join(alt for _, alt in _OP_ALTERNATIVES) + r")"
    r"\s*"
    r"(?P<value>'[^']*'|\"[^\"]*\"|「[^」]*」|"
    r"(?:" + _DATE_CN + r")|"
    r"(?:" + _DATE_ISO + r")|"
    r"-?\d+(?:\.\d+)?|"
    r"[^\s,，。;；)）\]\【]+)"
)

# ref 占位本身
_REF_PLACEHOLDER_RE = re.compile(r"\[\[ref:(r\d+)(?:\|[^]]+)?\]\]")

# ref 后允许的"小词"窗口(如 `的值是`、`为`、`时`),用于在 ref 与 op 之间容忍间隔
_REF_OP_GAP = r"[^\s,，。;；)）\]\【]{0,8}"


def _normalize_date(value: str) -> str | None:
    """把 `2026年7月` / `2026 年 7 月` / `2026年07月5日` / `2026-7` / `2026/07/05`
    统一为 ISO 字符串(容忍数字与年/月/日之间的空格)。"""
    s = value.strip()
    m = re.match(r"^(\d{4})\s*年\s*(\d{1,2})\s*月\s*$", s)
    if m:
        y, mo = m.group(1), int(m.group(2))
        return f"{y}-{mo:02d}"
    m = re.match(r"^(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*$", s)
    if m:
        y, mo, d = m.group(1), int(m.group(2)), int(m.group(3))
        return f"{y}-{mo:02d}-{d:02d}"
    m = re.match(r"^(\d{4})[-/](\d{1,2})(?:[-/](\d{1,2}))?$", s)
    if m:
        y, mo = m.group(1), int(m.group(2))
        d = m.group(3)
        iso = f"{y}-{mo:02d}"
        if d:
            iso += f"-{int(d):02d}"
        return iso
    return None


def _parse_literal(value: str) -> Any:
    """从文本中抽出的 value 字符串转成 Python 字面量。"""
    if not value:
        return None
    if (value[0] in "'\"「" and value[-1] in "'\"」"):
        return value[1:-1]
    iso = _normalize_date(value)
    if iso:
        return iso
    if re.match(r"^-?\d+(?:\.\d+)?$", value):
        return float(value) if "." in value else int(value)
    # 布尔/空
    if value in ("true", "True", "真"):
        return True
    if value in ("false", "False", "假"):
        return False
    if value in ("null", "None", "空"):
        return None
    return value


def _extract_conditions_from_text(segments: list[dict], refs: list[dict]) -> list[dict]:
    """从渲染文本中扫描条件,生成比较条件列表。支持三种自然语言形态:
    1. `[[ref]] <op> <value>`(ref 紧跟 op,如 `@状态 为已支付`)
    2. `[[ref]] <小词窗口> <op> <value>`(ref 与 op 之间隔 `的值是`、`为` 等,如 `@状态 的值是已支付`)
    3. `<op> <value> <小词> [[ref]]`(op 在 ref 之前,如 `大于 2026年7月 的 @时间`)
    4. `[[ref]] <裸值>`(ref 后直接跟引号/「」值,无 op,默认 `=`)

    按 ref 在文本中的出现顺序返回;同一 ref 只取第一个命中的条件。
    """
    text = _segments_to_text(segments, refs)
    valid_ids = {r["ref_id"] for r in refs}
    conds: list[dict] = []
    seen: set[str] = set()
    # 形态 1+2:ref 后窗口内找 op + value
    for m in _REF_PLACEHOLDER_RE.finditer(text):
        rid = m.group(1)
        if rid not in valid_ids or rid in seen:
            continue
        end = m.end()
        window = text[end:end + 32]
        # 在窗口里找第一个 op(但跳过嵌套在另一个 [[ref:...]] 占位内的冒号/等号)
        op_m = _OP_ANY_RE.search(window)
        if op_m:
            op_text = op_m.group(0)
            gap = window[: op_m.start()]
            # 若 gap 内包含另一个 ref 占位,说明匹配到的 op 属于后面的 ref,跳过
            if "[[ref" in gap:
                op_m = None  # 强制跳过,进入形态 4 检查
        if op_m:
            op_text = op_m.group(0)
            # op 与 ref 之间的小词窗口长度限制
            gap = window[: op_m.start()]
            if len(gap.strip()) > 8 and not gap.strip().startswith(("的", "为", "是", ":", "：")):
                op_m = None  # 跳过
        if op_m:
            # op 之后找 value
            rest = window[op_m.end():]
            val_m = _VALUE_ANY_RE.match(rest) or _VALUE_ANY_RE.search(rest)
            if val_m:
                # 值不能是另一个 ref 占位的开头
                val_text = val_m.group(0)
                if not val_text.startswith("[[ref:"):
                    op = _match_op(op_text)
                    value = _parse_literal(val_text)
                    conds.append({"left": _ref_expr(rid), "op": op, "right": _literal_expr(value)})
                    seen.add(rid)
                    continue
        # 形态 4:ref 后直接跟引号/「」值(无 op)→ 默认 `=`
        bare = text[end:end + 16].lstrip()
        if bare and bare[0] in "'\"「":
            val_m = _VALUE_ANY_RE.match(bare)
            if val_m:
                value = _parse_literal(val_m.group(0))
                conds.append({"left": _ref_expr(rid), "op": "=", "right": _literal_expr(value)})
                seen.add(rid)
                continue
    # 形态 3:op + value 在 ref 之前(如 `大于 2026年7月 的 @时间`)
    for m in _REF_PLACEHOLDER_RE.finditer(text):
        rid = m.group(1)
        if rid not in valid_ids or rid in seen:
            continue
        start = m.start()
        window = text[max(0, start - 40):start]
        # 在窗口尾部找 op + value(倒序匹配:value 紧邻 ref,op 在 value 前)
        tail_re = re.compile(
            r"(?P<op>" + "|".join(alt for _, alt in _OP_ALTERNATIVES) + r")"
            r"\s*"
            r"(?P<value>'[^']*'|\"[^\"]*\"|「[^」]*」|"
            r"(?:" + _DATE_CN + r")|"
            r"(?:" + _DATE_ISO + r")|"
            r"-?\d+(?:\.\d+)?|"
            r"[^\s,，。;；)）\]\【]+)"
            r"\s*(?:的|之)?\s*$"
        )
        tm = tail_re.search(window)
        if tm:
            op = _match_op(tm.group("op"))
            value = _parse_literal(tm.group("value"))
            conds.append({"left": _ref_expr(rid), "op": op, "right": _literal_expr(value)})
            seen.add(rid)
    return conds


def _match_op(op_text: str) -> str:
    for i, (op, _) in enumerate(_OP_ALTERNATIVES):
        if re.match(_OP_ALTERNATIVES[i][1], op_text):
            return op
    return "="


def _inject_heuristic_filter(body: dict, segments: list[dict], refs: list[dict]) -> dict:
    """metric 启发式补救:
    1. 优先从文本中抽取 `<ref> <op> <value>` 真实条件作为 filter;
    2. 抽不到但存在过滤关键词且 args 之外有未使用 ref 时,退化为 `=` + null 占位。
    避免因 LLM 漏识别导致 filter 为空。"""
    if not isinstance(body, dict):
        return body
    text = _segments_to_text(segments, refs)
    # 1) 文本条件抽取(对 `大于 2026年7月` 这类隐式过滤尤其重要)
    extracted = _extract_conditions_from_text(segments, refs)
    # 仅保留未在 args/group_by 中作为聚合/维度主体的 ref 条件,避免误把度量字段也塞进 filter
    args_ids: set[str] = set()
    args = body.get("args") or []
    if isinstance(args, list):
        for a in args:
            if isinstance(a, dict) and isinstance(a.get("ref"), str):
                args_ids.add(a["ref"])
    group_by_ids: set[str] = set()
    gb = body.get("group_by") or []
    if isinstance(gb, list):
        for a in gb:
            if isinstance(a, dict) and isinstance(a.get("ref"), str):
                group_by_ids.add(a["ref"])
    extracted = [
        c for c in extracted
        if (c.get("left", {}).get("ref") not in args_ids
            and c.get("left", {}).get("ref") not in group_by_ids)
    ]
    if extracted:
        body["filter"] = (
            extracted[0] if len(extracted) == 1
            else {"op": "and", "conditions": extracted}
        )
        return body
    # 2) 关键词命中 + 未使用 ref 退化兜底
    if not _FILTER_KEYWORD_RE.search(text):
        return body
    used_in_order: list[str] = []
    for seg in segments:
        if seg.get("type") == "ref" and seg.get("ref_id") not in used_in_order:
            used_in_order.append(seg.get("ref_id"))
    candidate_ids = [
        rid for rid in used_in_order
        if rid not in args_ids and rid not in group_by_ids
    ]
    if not candidate_ids:
        return body
    conds = [
        {"left": _ref_expr(rid), "op": "=", "right": _literal_expr(None)}
        for rid in candidate_ids
    ]
    body["filter"] = (
        conds[0] if len(conds) == 1 else {"op": "and", "conditions": conds}
    )
    return body


_AST_SCHEMA_DOC = """{
  "type": "metric | tag | rule",
  "description": "一句话说明(可省略,沿用入参 description)",
  "refs": [
    {
      "ref_id": "rN",
      "object_type_id": "对象类型 id",
      "object_name": "对象标识名(英文)",
      "object_display_name": "对象语义名(中文)",
      "property_id": "属性 id(可空)",
      "property_name": "属性标识名(可空)",
      "property_display_name": "属性语义名(可空)"
    }
  ],
  "body": <根据 type 选择下列结构之一>
}

# body 结构(按 type 取一个)
metric:
  {
    "operation": "sum | count | avg | min | max | distinct_count | custom",
    "args": [expr, ...],          // 聚合参数;count 可为 [] ;custom 表示非内置聚合
    "filter": condition | null,   // 过滤条件,无则为 null
    "group_by": [expr, ...],      // 分组维度,无则为 []
    "window": null                // 预留,目前恒为 null
  }

tag:
  { "cases": [ { "when": condition | null, "then": expr }, ... ] }  // when=null 表示 else 分支

rule:
  { "condition": condition, "message": "规则不满足时的提示文案" }

# 表达式
expr =
  { "ref": "rN" }                       // 引用 refs 中的某条;rN 必须存在于 refs
  | { "value": <字面量> }               // 数值不加引号,字符串加引号,布尔/空直接写字面量
  | { "op": "<运算符>", "args": [expr, ...] }  // 算术/字符串等运算: + - * / || 等
  | { "expr": expr }                    // 嵌套包裹(可选)

# 条件
condition =
  { "op": "and | or", "conditions": [condition, ...] }   // 组合条件,必须用 conditions
  | { "left": expr, "op": "<比较符>", "right": expr }    // 比较符: = != > >= < <= in like not_in
  | null
"""

_FEW_SHOT_EXAMPLES = """# 示例 1:伪代码风格(metric)
输入 natural_language_expression:
  "SUM([[ref:r1|订单.金额]]) 万元,其中 [[ref:r2|订单.状态]] = '已支付',按 [[ref:r3|订单.城市]] 分组"
refs: [r1=订单.金额, r2=订单.状态, r3=订单.城市]
输出:
{
  "type": "metric",
  "description": "",
  "refs": [ {r1...}, {r2...}, {r3...} ],
  "body": {
    "operation": "sum",
    "args": [ {"ref": "r1"} ],
    "filter": { "left": {"ref": "r2"}, "op": "=", "right": {"value": "已支付"} },
    "group_by": [ {"ref": "r3"} ],
    "window": null
  }
}

# 示例 2:自然语言风格(metric)
输入 natural_language_expression:
  "统计每个城市的已支付订单总金额,金额取自 [[ref:r1|订单.金额]],状态字段为 [[ref:r2|订单.状态]],按 [[ref:r3|订单.城市]] 拆分"
输出 body 同示例 1(operation=sum, args=[r1], filter={r2='已支付'}, group_by=[r3])。

# 示例 3:tag 风格
输入 natural_language_expression:
  "若 [[ref:r1|用户.年龄]] >= 18 标记为成年,否则未成年"
输出:
{
  "type": "tag",
  "description": "",
  "refs": [ {r1...} ],
  "body": {
    "cases": [
      { "when": { "left": {"ref": "r1"}, "op": ">=", "right": {"value": 18} }, "then": {"value": "成年"} },
      { "when": null, "then": {"value": "未成年"} }
    ]
  }
}

# 示例 4:rule 风格
输入 natural_language_expression:
  "当 [[ref:r1|订单.金额]] > 10000 时触发风控"
输出:
{
  "type": "rule",
  "description": "",
  "refs": [ {r1...} ],
  "body": {
    "condition": { "left": {"ref": "r1"}, "op": ">", "right": {"value": 10000} },
    "message": "规则不满足"
  }
}
"""


class ExpressionFormatterService:
    """将 expression_draft 格式化为统一 AST JSON。

    构造方式与 ``LogicImportService`` 一致:接收一个 ``LlmRuntimeConfig`` 或
    默认从 ``SettingsService`` 取默认 LLM。
    """

    def __init__(self, runtime_config=None) -> None:
        if runtime_config is None:
            self.use_mock = settings.use_mock_llm or not settings.openai_api_key
            self.client = (
                OpenAI(api_key=settings.openai_api_key) if not self.use_mock else None
            )
            self.model = settings.openai_model
        else:
            self.use_mock = runtime_config.use_mock or not runtime_config.api_key
            self.client = (
                OpenAI(
                    api_key=runtime_config.api_key,
                    base_url=runtime_config.api_base_url,
                )
                if not self.use_mock
                else None
            )
            self.model = runtime_config.model

    async def format(
        self,
        db: Session,
        *,
        domain_id: str,
        expression_draft: dict,
        logic_type: str | None = None,
        description: str | None = None,
    ) -> dict:
        domain = db.get(DomainContext, domain_id)
        if not domain:
            raise ValueError("数据域不存在")
        ontology = OntologyQueryService().get_published_ontology(db, domain_id)
        if not ontology:
            raise ValueError("该数据域尚无已发布本体,无法格式化表达式")

        segments, refs = _parse_draft(expression_draft)
        if not segments:
            raise ValueError("表达式草稿为空")
        refs = _resolve_refs(db, ontology.id, refs)

        if self.use_mock:
            ast = _mock_format(segments, refs, logic_type, description)
        else:
            ast = await asyncio.to_thread(
                self._format_with_llm, segments, refs, logic_type, description
            )

        summary = _segments_to_summary(segments, refs)
        return {
            "expression_json": ast,
            "expression_summary": summary,
        }

    def _format_with_llm(
        self,
        segments: list[dict],
        refs: list[dict],
        logic_type: str | None,
        description: str | None,
    ) -> dict:
        text = _segments_to_text(segments, refs)
        refs_payload = [
            {
                "ref_id": r["ref_id"],
                "object_type_id": r.get("object_type_id"),
                "object_name": r.get("object_name"),
                "object_display_name": r.get("object_display_name"),
                "property_id": r.get("property_id"),
                "property_name": r.get("property_name"),
                "property_display_name": r.get("property_display_name"),
            }
            for r in refs
        ]
        # 给 LLM 一份 refs 索引,便于在伪代码/自然语言混合时定位引用
        refs_index = "\n".join(
            f"- {r['ref_id']}: "
            + (".".join(filter(None, [r.get("object_display_name") or r.get("object_name"),
                                       r.get("property_display_name") or r.get("property_name")]))
               if (r.get("property_id") or r.get("property_name"))
               else (r.get("object_display_name") or r.get("object_name") or "?"))
            for r in refs_payload
        ) or "(无引用)"
        prompt = json.dumps(
            {
                "natural_language_expression": text,
                "logic_type": logic_type,
                "description": description,
                "refs": refs_payload,
                "refs_index": refs_index,
            },
            ensure_ascii=False,
            indent=2,
        )
        response = self.client.chat.completions.create(
            model=self.model,
            temperature=0,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是企业业务逻辑建模专家,负责把用户在富文本编辑器里组合的"
                        "表达式草稿解析为统一的 AST 风格 JSON。\n\n"
                        "## 输入说明\n"
                        "用户表达式由两部分组成:\n"
                        "1. 对本体对象/属性的引用,在文本中已用 `[[ref:rN|对象.属性]]` "
                        "占位标记好,并同步给出 refs 清单与 refs_index。\n"
                        "2. 其余文本可能是以下任意一种(或混合):伪代码 / SQL 风格、"
                        "自然语言、公式 / 算术。\n"
                        "请基于你对业务语义的理解,自行识别其中的聚合、过滤、分组、"
                        "分支、规则等意图,映射到 AST。不要依赖固定关键词列表。\n\n"
                        "## 输出 schema(严格遵循)\n"
                        f"{_AST_SCHEMA_DOC}\n\n"
                        "## 硬性要求\n"
                        "1. body 中所有引用必须用 `{\"ref\": \"rN\"}` 形式,且 rN 必须出现在"
                        "传入 refs 清单中,严禁编造新 ref_id 或新对象/字段。\n"
                        "2. 不得在 body 中直接写对象名/字段名,只能用 ref。字面量用 "
                        "`{\"value\": ...}`,数值不加引号。\n"
                        "3. refs 数组原样回填(可省略未在 body 中使用到的引用,但不要修改其内容)。\n"
                        "4. 组合条件必须用 `\"conditions\"` 字段,不要写成 \"items\" / \"clauses\"。\n"
                        "5. 识别出的过滤条件放入 metric body 的 `filter` 字段"
                        "(不要用 where/having/predicate/conditions 等其他键名);"
                        "tag 用 `cases[].when`,rule 用 `condition`。"
                        "若文本未明确给出比较右值,用 `{\"value\": null}` 占位。\n"
                        "6. 若表达式无明确聚合/过滤/分组语义,按 logic_type(默认 metric)给出"
                        "最合理结构,可在 description 里补充说明。\n"
                        "7. 只返回 JSON 对象本身,不要任何解释文字、不要 markdown 代码块。\n\n"
                        "## 示例(仅示意输出结构,识别逻辑请自行判断)\n"
                        f"{_FEW_SHOT_EXAMPLES}"
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content or "{}"
        try:
            raw = json.loads(content)
        except json.JSONDecodeError:
            logger.warning("expression_formatter: LLM 返回非法 JSON,回退 mock。raw=%s", content[:500])
            raw = _mock_format(segments, refs, logic_type, description)
        logger.info(
            "expression_formatter: LLM 原始返回 body=%s",
            json.dumps(raw.get("body"), ensure_ascii=False)[:500],
        )
        return self._normalize_llm_output(raw, segments, refs, logic_type, description)

    def _normalize_llm_output(
        self,
        raw: dict,
        segments: list[dict],
        refs: list[dict],
        logic_type: str | None,
        description: str | None,
    ) -> dict:
        logic_type = (raw.get("type") or logic_type or "metric").lower()
        if logic_type not in {"metric", "tag", "rule"}:
            logic_type = "metric"
        body = raw.get("body") or {}
        if not isinstance(body, dict):
            body = {}
        # 用输入 refs 的语义信息建立 ref_id -> 完整 meta 的映射,用于补全 LLM 输出
        meta_by_id = {r["ref_id"]: _ref_meta(r) for r in refs}
        valid_ids = set(meta_by_id)
        # 修正 LLM 常见错误:把组合条件的 items/clauses 字段名改成 conditions
        body = _rewrite_condition_keys(body)
        # metric:统一 filter 字段,LLM 可能写成 where/having/predicate 等同义词
        if logic_type == "metric":
            body = _unify_metric_filter(body)
        # 扫描 body 中所有 {"ref": "rN"},剔除不存在的 ref_id
        used_ids = _collect_ref_ids(body)
        bogus = used_ids - valid_ids
        if bogus:
            body = _strip_unknown_refs(body, valid_ids)
        # metric:filter 可能因 strip 变成无效(左值非 ref),或 LLM 漏识别;
        # 此时从文本启发式抽取条件作为兜底
        if logic_type == "metric":
            if not _filter_has_valid_ref(body.get("filter"), valid_ids):
                body["filter"] = None
                body = _inject_heuristic_filter(body, segments, refs)
            else:
                # LLM 产出了合法 ref 的条件,但比较右值可能为 null(未识别出值);
                # 用启发式抽取填补 null 值
                body = _enhance_filter_null_values(body, segments, refs)
        out_refs: list[dict] = []
        seen: set[str] = set()
        for r in raw.get("refs") or []:
            if not (isinstance(r, dict) and r.get("ref_id") in valid_ids):
                continue
            rid = r["ref_id"]
            seen.add(rid)
            # 以输入 meta 为准,LLM 输出的 id/display 字段只作兜底
            base = dict(meta_by_id[rid])
            base["object_type_id"] = r.get("object_type_id") or base.get("object_type_id")
            base["property_id"] = r.get("property_id") or base.get("property_id")
            out_refs.append(base)
        # 补全 LLM 漏掉的引用(按输入顺序,保留全部,便于前端展示)
        for r in refs:
            if r["ref_id"] not in seen:
                out_refs.append(dict(meta_by_id[r["ref_id"]]))
        result = {
            "type": logic_type,
            "description": (raw.get("description") or description or "").strip(),
            "refs": out_refs,
            "body": body,
        }
        logger.info(
            "expression_formatter: 归一化后 body=%s",
            json.dumps(body, ensure_ascii=False)[:500],
        )
        return result
