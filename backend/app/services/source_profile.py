"""可插拔「源画像」(source profile)：把不同元数据源的建库约定翻译成分类管线
已理解的抽象（主键 / 外键 / 子表 / 系统列），从而无需为每个源改分类器本身。

背景：真实 DataHub 常常**不声明** PK/FK（如 ERPNext/Frappe 只把 `name` 当主键、
用 Link 字段而非 `*_id` 外键、明细表靠 `parent`/`parenttype` 列标识）。若直接喂给
分类器，会因结构信号缺失把绝大多数表默认成低置信业务对象。源画像在证据组装前
补齐这些结构信号：

- `DefaultProfile`：不做任何推断，保持既有行为（声明式 PK/FK 原样透传）。
- `FrappeProfile`：ERPNext/Frappe 约定——`name`=主键、剥离框架标准列、
  `parent/parenttype/parentfield`=子/明细表、Link 字段(列名==某 DocType 名)=推断外键。

`detect_source_profile(bundle)` 按内容嗅探选用画像（不看单表名，看全域特征）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.schemas import DataHubDomainBundle, DatasetInput


@dataclass(frozen=True)
class InferredFk:
    """由源画像推断出的一条外键边（列 → 目标表）。"""

    column: str
    target_table: str


def _norm_token(name: str) -> str:
    """归一化为可比较的 token：小写、去除所有非字母数字。"""
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


class SourceProfile:
    """默认画像：不推断任何东西，保持声明式元数据原样（向后兼容）。"""

    name = "default"

    def primary_key_names(self, dataset: DatasetInput) -> set[str]:
        """该表的主键列名（小写）。默认取声明式 is_primary_key。"""
        return {f.name.lower() for f in dataset.fields if f.is_primary_key}

    def is_system_column(self, column: str) -> bool:
        """是否为框架/审计系统列（分类时应排除，避免稀释业务字段占比）。"""
        return False

    def is_child_table(self, dataset: DatasetInput) -> bool:
        """是否为明细/子表（隶属某父表，不是独立业务实体）。"""
        return False

    def build_table_index(self, bundle: DataHubDomainBundle) -> dict[str, str]:
        """归一化目标名 → 真实表名，供 Link 字段推断外键。默认空（不推断）。"""
        return {}

    def inferred_fks(
        self, dataset: DatasetInput, table_index: dict[str, str]
    ) -> list[InferredFk]:
        """由命名约定推断的外键边。默认无。"""
        return []


class FrappeProfile(SourceProfile):
    """ERPNext / Frappe 约定画像。"""

    name = "frappe"

    # Frappe 框架标准列（审计/元数据/子表锚点）：非业务属性，分类前剥离。
    STANDARD_COLUMNS = frozenset(
        {
            "name",  # 主键（单独作为 PK 处理，不当业务属性）
            "creation",
            "modified",
            "modified_by",
            "owner",
            "docstatus",
            "idx",
            "_user_tags",
            "_comments",
            "_assign",
            "_liked_by",
            "_seen",
            "parent",
            "parenttype",
            "parentfield",
            "naming_series",
            "amended_from",
        }
    )

    # 子表锚点列：出现即说明该表是某父 DocType 的明细子表。
    _CHILD_MARKERS = frozenset({"parenttype", "parentfield"})

    def primary_key_names(self, dataset: DatasetInput) -> set[str]:
        cols = {f.name.lower() for f in dataset.fields}
        pk = {f.name.lower() for f in dataset.fields if f.is_primary_key}
        if "name" in cols:
            pk.add("name")  # Frappe 以 name 为字符串主键
        return pk

    def is_system_column(self, column: str) -> bool:
        c = (column or "").lower()
        # name 虽在标准列里，但它是主键，交由 PK 逻辑处理，不算系统噪声列。
        if c == "name":
            return False
        return c in self.STANDARD_COLUMNS

    def is_child_table(self, dataset: DatasetInput) -> bool:
        cols = {f.name.lower() for f in dataset.fields}
        return bool(self._CHILD_MARKERS & cols)

    def _doctype_token(self, dataset: DatasetInput) -> str:
        base = dataset.name
        if base.lower().startswith("tab"):
            base = base[3:]
        return _norm_token(base)

    def build_table_index(self, bundle: DataHubDomainBundle) -> dict[str, str]:
        index: dict[str, str] = {}
        for ds in bundle.datasets:
            tok = self._doctype_token(ds)
            if len(tok) >= 3:  # 避免过短 token 误匹配
                index.setdefault(tok, ds.name)
        return index

    def inferred_fks(
        self, dataset: DatasetInput, table_index: dict[str, str]
    ) -> list[InferredFk]:
        """Frappe Link 字段：列名归一化后命中某 DocType 名 → 指向该表的外键。
        排除标准列、排除自指。"""
        out: list[InferredFk] = []
        seen: set[str] = set()
        for f in dataset.fields:
            col = f.name.lower()
            if col in self.STANDARD_COLUMNS:
                continue
            tok = _norm_token(col)
            if len(tok) < 3:
                continue
            target = table_index.get(tok)
            if target and target != dataset.name and f.name not in seen:
                out.append(InferredFk(column=f.name, target_table=target))
                seen.add(f.name)
        return out


def detect_source_profile(bundle: DataHubDomainBundle) -> SourceProfile:
    """按全域内容特征选用画像（不依赖单表名）。

    Frappe 特征：多数表以 `tab` 前缀命名，且普遍含 `name`/`creation`/`modified`
    这组框架标准列。命中过半即判定为 Frappe 源。
    """
    datasets = bundle.datasets
    if not datasets:
        return SourceProfile()
    total = len(datasets)
    tab_like = sum(1 for d in datasets if d.name.lower().startswith("tab"))
    frappe_std = sum(
        1
        for d in datasets
        if {"name", "creation", "modified"} <= {f.name.lower() for f in d.fields}
    )
    if tab_like >= total * 0.5 and frappe_std >= total * 0.5:
        return FrappeProfile()
    return SourceProfile()
