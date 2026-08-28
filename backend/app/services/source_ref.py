"""``ObjectType.source_ref`` 的判读 —— 本体的三种来源在这里分开。

本体有三个来源，只有 ``source_ref`` 的**形态**能把它们区分开：

- **DataHub 采集**：``urn:li:dataset:(urn:li:dataPlatform:<平台>,<库>.<表>,PROD)``。
  背后有真实的物理源表，可以搬数据 → 同步。
- **人工建模**：``manual:<数据源|方言>:<标识>``（只有 ``ManualCreationService`` 产出）。
  任何库里都没有表，只有元数据 → 只能物化建表，没有可搬的源。
- **派生建模**：``derived:<本体 id>:<标识>``（只有 ``derived_object`` 产出）。
  上游在**数仓里**（几张 ODS/DWD 表 join 出的新粒度），不在源库里 → 同样不能同步，
  但它有确定的上游：那份声明在 ``DerivedDefinition``，不在 ``source_ref`` 里。

三者对「能不能同步」的答案是一样的（只有 datahub 能），但对「数据从哪来」不一样：
人工建模没有上游，派生建模有——所以它们不能共用一个 ``manual`` 标签，否则界面只能对
派生对象说「无源表」，而它的源明明就在数仓里躺着。

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
DERIVED_PREFIX = "derived:"
_URN_PREFIX = "urn:li:dataset:"

Provenance = Literal["datahub", "manual", "derived", "none"]

# 「这个对象没有可搬的源」的统一说法。三种成因（无 source_ref / 手工建模 / 派生建模）
# 对搬运是同一件事：没有源库表可搬。但**派生对象要单独说**——它有上游，只是上游在数仓里，
# 对它说「无源表」会让人以为建模缺了东西而去补一个不存在的数据源。
NO_PHYSICAL_SOURCE_NOTE = "对象无可定位的物理源表（无 source_ref，或为手工建模对象），不产搬运作业"
NO_SOURCE_NOTE_BY_PROVENANCE = {
    "manual": "手工建模对象（库里没有对应的源表），只能物化建表，不产搬运作业",
    "derived": "派生对象（上游是数仓里的数据集，见派生定义），由清洗任务落数，不产搬运作业",
    "none": NO_PHYSICAL_SOURCE_NOTE,
}


def is_manual_source_ref(ref: str | None) -> bool:
    """该引用是否指向人工建模对象（无物理源表）。"""
    return bool(ref) and ref.startswith(MANUAL_PREFIX)


def is_derived_source_ref(ref: str | None) -> bool:
    """该引用是否指向派生对象（上游在数仓里，见 ``DerivedDefinition``）。"""
    return bool(ref) and ref.startswith(DERIVED_PREFIX)


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
    """本体对象的来源：只有 ``datahub`` 有源库表可搬；``derived`` 的上游在数仓里，
    ``manual`` / ``none`` 没有上游。

    结构不完整的 URN 归 ``none`` 而非 ``datahub``：它自称采集而来，但我们无从据此定位
    任何东西，把这个「声称」当事实正是要消除的那类错误。
    """
    if is_dataset_urn(ref):
        return "datahub"
    if is_manual_source_ref(ref):
        return "manual"
    if is_derived_source_ref(ref):
        return "derived"
    return "none"


def has_physical_source(ref: str | None) -> bool:
    """是否存在**可搬运的源库表**。同步的准入条件，物化不看这个。

    派生对象在这里同样是 False：它的上游是数仓里的表，由清洗任务按
    ``DerivedDefinition`` 读取，不走「从源库搬进 ODS」那条路。
    """
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
    "DERIVED_PREFIX",
    "MANUAL_PREFIX",
    "NO_PHYSICAL_SOURCE_NOTE",
    "NO_SOURCE_NOTE_BY_PROVENANCE",
    "Provenance",
    "has_physical_source",
    "is_dataset_urn",
    "is_derived_source_ref",
    "is_manual_source_ref",
    "manual_dialect_of",
    "provenance_of",
    "source_platform_of",
    "source_table_of",
]
