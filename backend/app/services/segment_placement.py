"""把没有板块归属的对象放进板块——「每个对象恰好属于一个板块」的守门人。

规矩只有一句：**是业务对象或业务关系表的，一定落在某个业务板块下；其余的落系统表。**
中间地带一个都不留。以前判成业务对象却连不成簇的表会掉进「待归类业务对象」，那是个
隐式垃圾桶——既进不了业务地图，也不出现在任何板块视图上，没人知道该拿它怎么办。

归位是**级联**的，先命中先归属：

1. 数据库自带 schema → 系统表（压根不该进本体）
2. 非业务角色（数据表 / 技术表）→ 系统表
3. 枢纽对象 → 公共主数据
4. 邻居投票：关系对端已经在某个业务板块里，就跟着去（最强信号，图上真的连着）
5. 命名族亲和：同族的表已经在某个业务板块里，就跟着去
   （``tabSales Invoice Item`` 跟着 ``tabSales Invoice`` 走）
6. 以上都兜不住 → 系统表，由人在审核台上移出来

第 6 步不是又一个垃圾桶：业务角色却落在系统表里的对象由 :func:`stranded_in_system`
数出来，审核台在系统表那一行挂红色上标，一次「移动到板块」就归位。落位本身**不动**
复核状态——那是判定，不是落位该管的事。机器不再留悬而未决的第三态。

对象不只从生成流水线一条路进来——人工建模、派生对象、编辑接口、发布快照都会直接
new 一个 ObjectType，它们的 ``segment_id`` 天然是空的。所以判定逻辑只留一份
（:func:`classify_fallback_kind` + :class:`AffinityIndex`），落库只走一个入口
（:func:`place_unsegmented` / :func:`resettle_by_role`），生成、创建、回填三条路共用。
"""

from __future__ import annotations

import json
import logging
from collections import Counter

from sqlalchemy.orm import Session

from app.models import ObjectType, OntologySegment, RelationType
from app.services.segment_kinds import (
    FALLBACK_KIND_ORDER,
    FALLBACK_SEGMENT_META,
    SEGMENT_KIND_BUSINESS,
    SEGMENT_KIND_SHARED,
    SEGMENT_KIND_SYSTEM,
    is_business_role,
    is_system_table,
)

logger = logging.getLogger(__name__)


def classify_fallback_kind(obj: ObjectType) -> str:
    """亲和推断兜不住时，这个对象的板块归宿。顺序即优先级。

    先判 system 再判角色：系统表几乎都会同时被判成 technical，但「压根不该进本体」
    比「是技术表」更能说明该怎么处置它。枢纽要求角色是业务的——技术表就算被处处引用
    也不是「公共主数据」。
    """
    if is_system_table(obj.source_ref):
        return SEGMENT_KIND_SYSTEM
    if not is_business_role(obj.table_role):
        return SEGMENT_KIND_SYSTEM
    if getattr(obj, "is_hub", False):
        return SEGMENT_KIND_SHARED
    return SEGMENT_KIND_SYSTEM


def segment_pinned_by_user(obj: ObjectType) -> bool:
    """人工是否钉死过这个对象的板块归属（手动移过板块）。"""
    try:
        return "segment_id" in set(json.loads(obj.overridden_fields or "[]"))
    except (TypeError, ValueError):
        return False


class AffinityIndex:
    """一个本体的业务板块亲和索引：邻居投票 + 命名族投票，各建一次。

    单点调用（编辑接口改一个对象的角色）与批量落位（生成后回填几百个孤儿）共用它，
    因此两条路给出的归位结果永远一致。
    """

    def __init__(self, db: Session, ontology_id: str):
        from app.services.review_queue import name_family

        business_segment_ids = {
            seg.id
            for seg in db.query(OntologySegment)
            .filter(
                OntologySegment.ontology_id == ontology_id,
                OntologySegment.kind.in_((SEGMENT_KIND_BUSINESS, SEGMENT_KIND_SHARED)),
            )
            .all()
        }
        self.business_segment_ids = business_segment_ids
        # 已经归进业务板块的对象：它们是投票人。
        self.segment_of: dict[str, str] = {}
        self.family_votes: dict[str, Counter] = {}
        rows = (
            db.query(ObjectType.id, ObjectType.name, ObjectType.segment_id)
            .filter(
                ObjectType.ontology_id == ontology_id,
                ObjectType.deleted_by_user == False,  # noqa: E712
                ObjectType.segment_id.isnot(None),
            )
            .all()
        )
        for row in rows:
            if row.segment_id not in business_segment_ids:
                continue
            self.segment_of[row.id] = row.segment_id
            self.family_votes.setdefault(name_family(row.name), Counter())[
                row.segment_id
            ] += 1

        # 邻接表：只需要「对端是谁」，方向无关。
        self.neighbors: dict[str, set[str]] = {}
        for src, tgt in (
            db.query(
                RelationType.source_object_type_id, RelationType.target_object_type_id
            )
            .filter(
                RelationType.ontology_id == ontology_id,
                RelationType.deleted_by_user == False,  # noqa: E712
            )
            .all()
        ):
            if not src or not tgt:
                continue
            self.neighbors.setdefault(src, set()).add(tgt)
            self.neighbors.setdefault(tgt, set()).add(src)

    def vote(self, obj: ObjectType) -> str | None:
        """这个对象该跟着哪个业务板块走；没有信号返回 ``None``。"""
        from app.services.review_queue import name_family

        votes = Counter(
            self.segment_of[n]
            for n in self.neighbors.get(obj.id, ())
            if n in self.segment_of
        )
        if votes:
            # 平票按板块 id 定序，同一批输入两次跑出同一个结果。
            return min(votes.most_common(), key=lambda kv: (-kv[1], kv[0]))[0]
        family = self.family_votes.get(name_family(obj.name))
        if family:
            return min(family.most_common(), key=lambda kv: (-kv[1], kv[0]))[0]
        return None

    def adopt(self, object_id: str, segment_id: str, name: str | None) -> None:
        """把刚归位的对象登记为投票人，让同族/相邻的后续对象跟着它走。"""
        from app.services.review_queue import name_family

        self.segment_of[object_id] = segment_id
        self.family_votes.setdefault(name_family(name), Counter())[segment_id] += 1


def ensure_fallback_segment(db: Session, ontology_id: str, kind: str) -> OntologySegment:
    """取（必要时建）该本体的某个兜底板块。标识名固定，因此可以按 name 幂等取。"""
    meta = FALLBACK_SEGMENT_META[kind]
    segment = (
        db.query(OntologySegment)
        .filter(
            OntologySegment.ontology_id == ontology_id,
            OntologySegment.name == meta["name"],
        )
        .first()
    )
    if segment is None:
        segment = OntologySegment(
            ontology_id=ontology_id,
            name=meta["name"],
            display_name=meta["display_name"],
            kind=kind,
            description=meta["description"],
            member_count=0,
            origin="machine",
        )
        db.add(segment)
        db.flush()
    return segment


def refresh_member_counts(db: Session, ontology_id: str, segment_ids) -> None:
    """重算给定板块的成员数（``None`` 会被忽略——那不是板块）。"""
    ids = {sid for sid in segment_ids if sid}
    if not ids:
        return
    for segment in (
        db.query(OntologySegment)
        .filter(
            OntologySegment.ontology_id == ontology_id,
            OntologySegment.id.in_(ids),
        )
        .all()
    ):
        segment.member_count = (
            db.query(ObjectType)
            .filter(
                ObjectType.ontology_id == ontology_id,
                ObjectType.segment_id == segment.id,
                ObjectType.deleted_by_user == False,  # noqa: E712
            )
            .count()
        )


def place_unsegmented(db: Session, ontology_id: str) -> dict[str, int]:
    """把该本体里所有没有板块归属的对象放进板块，返回各去处的落位计数。

    业务角色的孤儿先走亲和归位（邻居 → 命名族），兜不住的才落系统表。计数里
    ``business`` 那一项是被亲和收编进业务板块的数量。

    幂等：已归属的对象一个都不碰（包括人工挪过板块的）。没有孤儿时不建任何板块——
    没有系统表的本体不该凭空多出一个「系统表」板块。
    """
    orphans = (
        db.query(ObjectType)
        .filter(
            ObjectType.ontology_id == ontology_id,
            ObjectType.segment_id.is_(None),
            ObjectType.deleted_by_user == False,  # noqa: E712
        )
        .all()
    )
    if not orphans:
        return {}

    index = AffinityIndex(db, ontology_id)
    placed: dict[str, int] = {}
    touched: set[str] = set()
    buckets: dict[str, list[ObjectType]] = {}
    # 名字定序：亲和归位会把已归位的对象登记成新投票人，顺序不定结果就不定。
    for obj in sorted(orphans, key=lambda o: (o.name or "", o.id)):
        kind = classify_fallback_kind(obj)
        if kind == SEGMENT_KIND_SYSTEM and is_business_role(obj.table_role):
            target = index.vote(obj)
            if target:
                obj.segment_id = target
                index.adopt(obj.id, target, obj.name)
                placed[SEGMENT_KIND_BUSINESS] = placed.get(SEGMENT_KIND_BUSINESS, 0) + 1
                touched.add(target)
                continue
        buckets.setdefault(kind, []).append(obj)

    for kind in FALLBACK_KIND_ORDER:
        members = buckets.get(kind)
        if not members:
            continue
        segment = ensure_fallback_segment(db, ontology_id, kind)
        for obj in members:
            obj.segment_id = segment.id
        placed[kind] = len(members)
        touched.add(segment.id)

    db.flush()
    refresh_member_counts(db, ontology_id, touched)
    logger.info("补齐板块归属：%s", placed)
    return placed


def dissolve_legacy_segments(db: Session, ontology_id: str) -> int:
    """拆掉存量库里的「待归类业务对象」与「技术表」两个板块，返回被腾空的对象数。

    这两类板块已从划分里去掉（见 :mod:`app.services.segment_kinds`）。成员先腾成
    未归属，再由 :func:`place_unsegmented` 按新规则重新落位——业务角色的走亲和归位，
    其余的进系统表。人工在这两个板块上钉过的 ``segment_id`` 一并作废：板块都不存在了，
    钉住一个不存在的归属没有意义。

    幂等：没有旧板块时什么都不做。
    """
    from app.services.segment_kinds import LEGACY_KINDS

    legacy = (
        db.query(OntologySegment)
        .filter(
            OntologySegment.ontology_id == ontology_id,
            OntologySegment.kind.in_(LEGACY_KINDS),
        )
        .all()
    )
    if not legacy:
        return 0
    legacy_ids = [seg.id for seg in legacy]
    freed = (
        db.query(ObjectType)
        .filter(
            ObjectType.ontology_id == ontology_id,
            ObjectType.segment_id.in_(legacy_ids),
        )
        .update({"segment_id": None}, synchronize_session=False)
    )
    for seg in legacy:
        db.delete(seg)
    db.flush()
    logger.info("拆掉旧板块 %d 个，腾出 %d 个对象待重新落位", len(legacy), freed)
    return freed


def segment_kind_of(db: Session, segment_id: str | None) -> str | None:
    """板块的种类；未归属或板块已不存在都返回 ``None``。"""
    if not segment_id:
        return None
    segment = db.get(OntologySegment, segment_id)
    return segment.kind if segment else None


def stranded_in_system(db: Session, obj: ObjectType) -> bool:
    """业务角色的对象是不是压在系统表里——归错了地方，等着被人移出去。

    这不再是门禁（判定不会因此被拒），而是**待判的理由**：这类对象保持
    ``needs_review=True``，在审核台上带红色上标，一次「移动到板块」就归位。
    """
    return is_business_role(obj.table_role) and (
        segment_kind_of(db, obj.segment_id) == SEGMENT_KIND_SYSTEM
    )


def resettle_by_role(
    db: Session,
    obj: ObjectType,
    index: "AffinityIndex | None" = None,
    *,
    apply: bool = True,
) -> str | None:
    """按当前角色重新给对象定板块。返回目标板块的 **kind**（不需要挪动返回 ``None``）。

    这是「角色决定板块」这条不变量的执行者：改判成数据表/技术表的对象会被移到系统表，
    改判成业务对象/关系表的对象会走亲和归位去业务板块。**人工钉过板块的对象不动**
    ——手动「移动到板块」是人的判断，机器不该在下一次改判时把它推翻。

    不要求角色**变了**才调用：存量里有一批「角色早就改对了、板块却没跟着挪」的对象，
    它们再点一次同样的改判是空操作，不自愈就永远躺在错板块里。判定发生时顺手自愈，
    比让人手动挪板块可靠。

    ``apply=False`` 只算不写（回填脚本的 dry-run）：判定走的是同一段代码，
    预演出来的数才等于真跑出来的数。

    ``index`` 建一次要全表扫对象与关系。批量判定/回填脚本请自己建一个传进来，
    否则每个对象都重建一次索引（60 个一批就是 60 次全表扫）。
    """
    if segment_pinned_by_user(obj) or not obj.ontology_id:
        return None
    current = segment_kind_of(db, obj.segment_id)
    previous_segment_id = obj.segment_id

    natural = classify_fallback_kind(obj)
    if is_business_role(obj.table_role):
        if current == SEGMENT_KIND_BUSINESS:
            return None  # 已在业务模块里，聚类/人工的归属不该被一次改判推翻
        if natural == SEGMENT_KIND_SHARED:
            # 枢纽的归宿就是「公共主数据」，不能被亲和投票拽进某个单一模块——
            # 它被处处引用，投票几乎总能给出一个板块，进去就把大半张图粘成一块。
            if current == natural:
                return None
            target_kind, target_id = natural, None
        else:
            target_id = (index or AffinityIndex(db, obj.ontology_id)).vote(obj)
            if target_id is not None:
                target_kind = segment_kind_of(db, target_id)
            else:
                target_kind = natural
                if target_kind == current:
                    return None
                target_id = None
    else:
        # 非业务角色一律回系统表，业务板块里也不例外——「其余的分配在系统表」。
        if current == SEGMENT_KIND_SYSTEM:
            return None
        target_kind, target_id = SEGMENT_KIND_SYSTEM, None

    if not apply:
        return target_kind
    if target_id is None:
        target_id = ensure_fallback_segment(db, obj.ontology_id, target_kind).id
    if target_id == previous_segment_id:
        return None
    obj.segment_id = target_id
    refresh_member_counts(db, obj.ontology_id, {previous_segment_id, target_id})
    return target_kind
