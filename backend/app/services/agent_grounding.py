"""Data Agent 事实凭证账本（F4）：本轮问答工具**实际返回**的全部事实。

封闭世界假设（CWA）的载体：凡不在账本里的对象/字段/关系/口径/数值，一律视为
「本体未证实」，答案不得引用。账本只登记工具**真实返回**的结构化字段——LLM 说的
一个字都不入账。事后由 answer_verifier 拿答案逐条对账，不可证即拒答。

与现有 chat_bi 的运行级布尔 `grounded_hit` 相比，这里是**断言级**接地：不再「命中过
任意工具就放行」，而是「答案里每条断言都必须在账本里找到凭证」。

设计见 FORMAL_VALIDATION_IMPL.md 第三部分 §3.1。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ObjectFact:
    id: str
    name: str
    display_name: str


@dataclass(frozen=True)
class PropFact:
    object_id: str
    name: str
    display_name: str
    semantic_type: str | None


@dataclass(frozen=True)
class RelationFact:
    id: str
    name: str
    display_name: str


@dataclass(frozen=True)
class MetricFact:
    id: str
    name: str
    display_name: str
    ast: dict | None  # expression_json（结构化口径 AST）


def _norm(text: str | None) -> str:
    return (text or "").strip().lower()


def _numeric_tokens(value: Any) -> set[str]:
    """把一个单元格值归一为可比较的数字字符串集合（容纳千分位/小数尾零差异）。"""
    out: set[str] = set()
    if value is None:
        return out
    s = str(value).strip()
    if not s:
        return out
    raw = s.replace(",", "").replace("%", "").strip()
    try:
        f = float(raw)
    except (TypeError, ValueError):
        return out
    out.add(raw)
    if f == int(f):
        out.add(str(int(f)))  # 12.0 -> "12"
    # 归一到定点，去掉尾零：1234.50 -> "1234.5"
    out.add(("%f" % f).rstrip("0").rstrip("."))
    return out


class FactLedger:
    """本轮问答可被引用的全部事实。CWA：不在账本 = 不成立。"""

    def __init__(self) -> None:
        self.objects: dict[str, ObjectFact] = {}
        self.props: dict[tuple[str, str], PropFact] = {}  # (object_id, prop_name_lower)
        self.relations: dict[str, RelationFact] = {}
        self.metrics: dict[str, MetricFact] = {}
        self.cells: list[Any] = []              # run_sql 返回的所有单元格原值
        self._cell_numbers: set[str] = set()    # 归一后的数字集合
        self._name_index: set[str] = set()      # 所有实体的 name/display_name（归一小写）

    # ------------------------------------------------------------------ 登记

    def add_object_summary(self, obj: dict) -> None:
        oid = obj.get("id")
        if not oid:
            return
        fact = ObjectFact(id=oid, name=obj.get("name") or "", display_name=obj.get("display_name") or "")
        self.objects[oid] = fact
        self._index_names(fact.name, fact.display_name)

    def add_object_detail(self, detail: dict) -> None:
        """get_object 返回：登记对象本身、其字段、其关系。"""
        oid = detail.get("id")
        if not oid:
            return
        self.add_object_summary(detail)
        for p in detail.get("properties") or []:
            pname = p.get("name")
            if not pname:
                continue
            pf = PropFact(
                object_id=oid,
                name=pname,
                display_name=p.get("display_name") or "",
                semantic_type=p.get("semantic_type"),
            )
            self.props[(oid, _norm(pname))] = pf
            self._index_names(pf.name, pf.display_name)
        for r in detail.get("relations") or []:
            self.add_relation(r)

    def add_relation(self, rel: dict) -> None:
        rid = rel.get("id")
        if not rid:
            return
        rf = RelationFact(id=rid, name=rel.get("name") or "", display_name=rel.get("display_name") or "")
        self.relations[rid] = rf
        self._index_names(rf.name, rf.display_name)

    def add_metric_summary(self, logic: dict) -> None:
        lid = logic.get("id")
        if not lid:
            return
        mf = MetricFact(
            id=lid, name=logic.get("name") or "", display_name=logic.get("display_name") or "",
            ast=logic.get("expression_json"),
        )
        self.metrics[lid] = mf
        self._index_names(mf.name, mf.display_name)

    def add_cells(self, columns: list, rows: list) -> None:
        """run_sql 实际返回的数据行：登记每个单元格值供数值断言核对。"""
        for row in rows or []:
            values = row.values() if isinstance(row, dict) else row
            for v in values:
                self.cells.append(v)
                self._cell_numbers |= _numeric_tokens(v)

    def _index_names(self, *names: str) -> None:
        for n in names:
            key = _norm(n)
            if key and len(key) >= 2:  # 单字符名不进索引，避免噪声误命中
                self._name_index.add(key)

    # ------------------------------------------------------------------ 判定

    @property
    def has_any_entity(self) -> bool:
        return bool(self.objects or self.metrics or self.relations)

    def has_entity_named(self, token: str) -> bool:
        """具名实体（对象/字段/关系/口径的 name 或 display_name）是否在账本。"""
        return _norm(token) in self._name_index

    def has_numeric(self, num_text: str) -> bool:
        """数值断言是否对得到某个 run_sql 单元格。"""
        return bool(_numeric_tokens(num_text) & self._cell_numbers)

    def metric_by_name(self, token: str) -> MetricFact | None:
        key = _norm(token)
        for m in self.metrics.values():
            if _norm(m.name) == key or _norm(m.display_name) == key:
                return m
        return None


__all__ = [
    "FactLedger",
    "ObjectFact",
    "PropFact",
    "RelationFact",
    "MetricFact",
]
