"""审核队列：把待复核对象聚成「一屏判一批」的组。

审核的工作单元不是一个对象，而是**一组同类对象**——同板块、同命名族、同判定强度的
表几乎总是同一个判定。这不是偏好而是数据决定的：``role_confidence`` 中位数 0.5、
绝大多数挤在 0.5–0.7，置信度排序没有信息量；866 个待复核逐个点开判要 5–7 小时，
成批裁决是唯一可行的路径。

这里只做**纯计算**：给一批轻量行，产出确定性排序的分组。确定性是全部意义所在——
``list_object_types`` 按 ``updated_at DESC`` 排序，而判定动作本身会改写 ``updated_at``
并把行移出 ``needs_review`` 结果集，于是 offset 分页会静默跳过整页。本模块的排序键
里没有任何随判定变化的字段，翻页因此可重放。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.services.object_classifier import ROLE_SCORE_THRESHOLD

# 命名前缀词元：只表明「这是一张表/哪一层」，对分族没有信息，剥掉再取族名。
# 例：tabPurchase Order / ods_sales_order → 族名分别是 purchase / sales。
_NAME_PREFIXES = frozenset(
    {"tab", "t", "tb", "dim", "fact", "fct", "ods", "dwd", "dws", "ads", "stg", "tmp", "temp"}
)

_CJK_RANGE = "一-鿿"
_CJK = re.compile(f"[{_CJK_RANGE}]")
# 一个词元 = 连续大写缩写 / 驼峰词 / 小写数字段 / 一段中文
_TOKEN = re.compile(f"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+|[{_CJK_RANGE}]+")

# 判定强度分带：机器的得分离阈值有多远，决定这一组能不能一次性放行。
# strong = 明显够格；near = 刚过线（最需要人看）；weak = 未过线，机器靠兜底规则留着的。
_BAND_STRONG = 3.0

BAND_LABELS = {
    "strong": "证据充分",
    "near": "刚过线",
    "weak": "未过线",
    "unknown": "无评分",
}

# 单组返回的成员上限：一屏判一批，几百条一次性铺出来既看不完也拖慢渲染。
# 超出部分由 ``truncated`` 标记，判完这一批下次进来会补上。
MAX_GROUP_MEMBERS = 60

# 成族的最小成员数。低于它的族并进同板块同角色同强度的「零散」桶。
#
# 命名族对「采购/销售」这种前缀家族很有效，但真实库里有大量各叫各名的表
# （MySQL 系统表 db/func/proc/servers…）。实测 erpnext 866 个待复核：
# 不并 460 组、328 个单成员组——那等于退回逐个判；并到 >=3 得 99 组、中位 4 个/组。
# 一次判定的成本从「点 460 次」降到「点 99 次」，这才是成组的意义。
MIN_FAMILY_SIZE = 3

# 零散桶的族名。同板块 + 同角色 + 同判定强度仍然成立，只是名字各不相同。
MISC_FAMILY = "*"


def tokenize_name(name: str | None) -> list[str]:
    """把标识名切成词元：分隔符、驼峰、中文段都切开，拉丁词元统一小写。"""
    text = (name or "").strip()
    if not text:
        return []
    tokens: list[str] = []
    for part in re.split(f"[^0-9A-Za-z{_CJK_RANGE}]+", text):
        if not part:
            continue
        for match in _TOKEN.finditer(part):
            token = match.group(0)
            tokens.append(token if _CJK.search(token) else token.lower())
    return tokens


def name_family(name: str | None) -> str:
    """标识名的「命名族」：同族的表通常同判。

    剥掉层级/表前缀后取第一个实词；中文名取前两字（采购/销售/库存 这类业务前缀）。
    取不出来时回落到原名的小写形式——宁可自成一族，也不要把无关的表凑一堆。
    """
    tokens = tokenize_name(name)
    while tokens and tokens[0] in _NAME_PREFIXES:
        tokens.pop(0)
    if not tokens:
        return (name or "").strip().lower()
    head = tokens[0]
    if _CJK.search(head):
        return head[:2]
    return head


def score_of(role_signals: dict | None) -> float | None:
    """从分类证据快照里取综合得分；存量对象可能没有快照。"""
    if not isinstance(role_signals, dict):
        return None
    score = role_signals.get("score")
    if isinstance(score, (int, float)):
        return float(score)
    return None


def score_band(score: float | None) -> str:
    """得分分带。阈值与 ``object_classifier.ROLE_SCORE_THRESHOLD`` 同源。"""
    if score is None:
        return "unknown"
    if score >= _BAND_STRONG:
        return "strong"
    if score >= ROLE_SCORE_THRESHOLD:
        return "near"
    return "weak"


@dataclass
class QueueRow:
    """队列里的一行（只取分组需要的列，不构造完整摘要）。"""

    id: str
    name: str
    display_name: str
    segment_id: str | None
    table_role: str
    role_signals: dict | None = None
    # 显式指定族名，跳过按标识名切词。关系用「动词」本身作族——切词会把
    # 「发起支付」和「发起审批」并成一族，而动词的整体才是它的身份。
    family: str | None = None
    # 直接给分（关系用 source_confidence，没有 role_signals）。
    score: float | None = None


@dataclass
class ReviewGroup:
    """一组同类待复核对象——审核的最小工作单元。"""

    key: str
    segment_id: str | None
    segment_name: str
    table_role: str
    name_family: str
    score_band: str
    member_ids: list[str] = field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.member_ids)


def group_key(
    segment_id: str | None, table_role: str, family: str, band: str
) -> str:
    """分组键：稳定、可作游标、可读。段内不会出现 ``|``（族名已被切词规整过）。"""
    return f"{segment_id or '-'}|{table_role}|{family or '-'}|{band}"


def sort_key(group: "ReviewGroup") -> tuple:
    """组的排序键。**只用不随判定变化的量**——这是游标能重放的前提。

    曾想按「板块待判数」降序排（活儿最多的板块先干），但那个数每判一个就会变，
    某个板块判到一半就可能被另一个板块顶到前面去，游标指向的位置随之漂移。
    「哪个板块还剩多少」交给左栏的进度地形表达，队列顺序保持稳定。
    """
    return (
        0 if group.segment_id else 1,
        group.segment_name,
        # 有名有姓的族排在零散桶前面：先判成片的，剩下的边角料最后收。
        1 if group.name_family == MISC_FAMILY else 0,
        group.name_family,
        group.table_role,
        group.score_band,
        group.key,
    )


def cursor_sort_key(
    cursor: str,
    *,
    segment_names: dict[str, str] | None = None,
    unsegmented_label: str = "未接入板块",
) -> tuple | None:
    """把游标（组 key）还原成排序键，用于「该组已判完消失」时定位到它原来的位置。"""
    parts = cursor.split("|")
    if len(parts) != 4:
        return None
    raw_segment, role, family, band = parts
    segment_id = None if raw_segment == "-" else raw_segment
    segment_name = (
        (segment_names or {}).get(segment_id, segment_id) if segment_id else unsegmented_label
    )
    return (
        0 if segment_id else 1,
        segment_name,
        1 if family == MISC_FAMILY else 0,
        family,
        role,
        band,
        cursor,
    )


def build_groups(
    rows: list[QueueRow],
    *,
    segment_names: dict[str, str] | None = None,
    unsegmented_label: str = "未接入板块",
) -> list[ReviewGroup]:
    """把待复核行聚成组并**确定性排序**（排序键见 ``sort_key``）。

    组内：得分高→低 → 显示名 → id。每一层都有确定的 tiebreaker，同样的库存必然
    给出同样的顺序，翻页才可重放。
    """
    names = segment_names or {}
    buckets: dict[str, ReviewGroup] = {}
    scores: dict[str, float | None] = {}
    row_by_id: dict[str, QueueRow] = {}

    def _score(row: QueueRow) -> float | None:
        return row.score if row.score is not None else score_of(row.role_signals)

    def _family(row: QueueRow) -> str:
        return row.family if row.family is not None else name_family(row.name or row.display_name)

    # 先数每个族有多少人，再决定它是自成一组还是并进零散桶。
    family_sizes: dict[tuple, int] = {}
    for row in rows:
        fam_key = (
            row.segment_id,
            row.table_role,
            score_band(_score(row)),
            _family(row),
        )
        family_sizes[fam_key] = family_sizes.get(fam_key, 0) + 1

    for row in rows:
        score = _score(row)
        band = score_band(score)
        family = _family(row)
        if family_sizes.get((row.segment_id, row.table_role, band, family), 0) < MIN_FAMILY_SIZE:
            family = MISC_FAMILY
        key = group_key(row.segment_id, row.table_role, family, band)
        group = buckets.get(key)
        if group is None:
            group = ReviewGroup(
                key=key,
                segment_id=row.segment_id,
                segment_name=(
                    names.get(row.segment_id, row.segment_id or unsegmented_label)
                    if row.segment_id
                    else unsegmented_label
                ),
                table_role=row.table_role,
                name_family=family,
                score_band=band,
            )
            buckets[key] = group
        group.member_ids.append(row.id)
        scores[row.id] = score
        row_by_id[row.id] = row

    for group in buckets.values():
        group.member_ids.sort(
            key=lambda oid: (
                -(scores.get(oid) if scores.get(oid) is not None else -99.0),
                row_by_id[oid].display_name or "",
                oid,
            )
        )

    return sorted(buckets.values(), key=sort_key)
