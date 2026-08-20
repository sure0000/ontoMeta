"""``ObjectType.source_ref`` 的判读 —— 本体的两种来源在这里分开。

本体有两个来源，只有 ``source_ref`` 的**形态**能把它们区分开：

- **DataHub 采集**：``urn:li:dataset:(urn:li:dataPlatform:<平台>,<库>.<表>,PROD)``。
  背后有真实的物理源表，可以搬数据 → 同步。
- **人工建模**：``manual:<数据源|方言>:<标识>``（只有 ``ManualCreationService`` 产出）。
  任何库里都没有表，只有元数据 → 只能物化建表，没有可搬的源。

``origin`` / ``user_created`` **不能**用来判别：``services/edit.py`` 会给带真实 URN 的对象打
``user_created=True``（从 DataHub 数据集补建承载对象），而每次改字段又会翻转 ``origin``。

与 ``connectors/datahub._extract_dataset_name`` 的分工：那个函数对非 URN 输入**原样返回**，
这对它自己的调用场景（解析 DataHub GraphQL 响应）是对的，但拿来解析 ``source_ref`` 就会把
``manual:mysql:customer_order`` 当成表名往下传。需要「真实表名」时用本模块的
``source_table_of``——它在解析不出时返回 ``None``，逼调用方显式处理。
"""

from __future__ import annotations

from typing import Literal

from app.connectors.datahub import _extract_dataset_name, _extract_platform

MANUAL_PREFIX = "manual:"
_URN_PREFIX = "urn:li:dataset:"

Provenance = Literal["datahub", "manual", "none"]

# 「这个对象没有可搬的源」的统一说法。两种成因（压根没有 source_ref / 是手工建模对象）
# 对使用者是同一件事：它只能物化建表，不能同步。分开写两句只会让人以为是两种故障。
NO_PHYSICAL_SOURCE_NOTE = "对象无可定位的物理源表（无 source_ref，或为手工建模对象），不产搬运作业"


def is_manual_source_ref(ref: str | None) -> bool:
    """该引用是否指向人工建模对象（无物理源表）。"""
    return bool(ref) and ref.startswith(MANUAL_PREFIX)


def is_dataset_urn(ref: str | None) -> bool:
    """该引用是否是**结构完整**的 DataHub dataset URN。

    只判前缀是不够的：``urn:li:dataset:不带括号`` 顶着正确前缀却解析不出库表与平台，
    当成「有源表」会一路走到搬运作业才炸。故这里要求平台与表名都能取出来 ——
    「看起来像 URN」和「能定位到源表」是两回事，本模块只认后者。
    """
    if not ref or not ref.startswith(_URN_PREFIX):
        return False
    return _dataset_name(ref) is not None and _extract_platform(ref) is not None


def _dataset_name(ref: str) -> str | None:
    """URN 里的 ``库.表``；解析不出（含 _extract_dataset_name 原样回吐）时 None。"""
    name = _extract_dataset_name(ref)
    return name if name and name != ref else None


def provenance_of(ref: str | None) -> Provenance:
    """本体对象的来源：``datahub`` 有源表可搬，``manual`` / ``none`` 没有。

    结构不完整的 URN 归 ``none`` 而非 ``datahub``：它自称采集而来，但我们无从据此定位
    任何东西，把这个「声称」当事实正是要消除的那类错误。
    """
    if is_dataset_urn(ref):
        return "datahub"
    if is_manual_source_ref(ref):
        return "manual"
    return "none"


def has_physical_source(ref: str | None) -> bool:
    """是否存在可搬运的物理源表。同步的准入条件，物化不看这个。"""
    return provenance_of(ref) == "datahub"


def source_table_of(ref: str | None) -> str | None:
    """源表名（``库.表``）。**解析不出时返回 None，绝不原样回吐输入。**

    与 ``_extract_dataset_name`` 的区别就在这里：那个函数把 ``manual:mysql:foo`` 原样返回，
    于是下游把它当表名拼进 SQL。要定位真实源表的地方一律用本函数。
    """
    return _dataset_name(ref) if is_dataset_urn(ref) else None


def source_platform_of(ref: str | None) -> str | None:
    """源平台（如 ``mariadb``），决定搬运用哪个连接器。非 URN 一律 None。"""
    return _extract_platform(ref) if is_dataset_urn(ref) else None


def manual_dialect_of(ref: str | None) -> str | None:
    """人工建模引用里记的数据源/方言：``manual:mysql:foo`` → ``mysql``。"""
    if not is_manual_source_ref(ref):
        return None
    parts = (ref or "").split(":", 2)
    return parts[1] or None if len(parts) >= 2 else None


__all__ = [
    "MANUAL_PREFIX",
    "NO_PHYSICAL_SOURCE_NOTE",
    "Provenance",
    "has_physical_source",
    "is_dataset_urn",
    "is_manual_source_ref",
    "manual_dialect_of",
    "provenance_of",
    "source_platform_of",
    "source_table_of",
]
