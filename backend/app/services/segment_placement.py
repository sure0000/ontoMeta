"""把没有板块归属的对象放进兜底板块——「每个对象恰好属于一个板块」的守门人。

生成流水线里 :func:`segment_generator.build_fallback_segments` 已经保证了这条不变量，
但对象不只从那一条路进来：人工建模、派生对象、编辑接口、发布快照都会直接 new 一个
ObjectType，它们的 ``segment_id`` 天然是空的。只在生成时保证，界面上就会慢慢又冒出
「未接入板块 N」——实测跑了一轮后台重生成 + 两个派生对象，就漏了 10 个。

所以判定逻辑只留一份（:func:`classify_fallback_kind`），落库只走一个入口
（:func:`place_unsegmented`），生成、创建、回填三条路共用。
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.models import ObjectType, OntologySegment
from app.services.segment_kinds import (
    FALLBACK_KIND_ORDER,
    FALLBACK_SEGMENT_META,
    SEGMENT_KIND_PENDING,
    SEGMENT_KIND_SHARED,
    SEGMENT_KIND_SYSTEM,
    SEGMENT_KIND_TECHNICAL,
    is_system_table,
)

logger = logging.getLogger(__name__)


#: 会留在「待归类业务对象」里的角色。别的角色说明它压根不该被当业务对象归类，
#: 归宿是「技术表」板块。判定只在这里写一次，队列的「这组必须先归位」也读它。
_ROLES_STAYING_PENDING = frozenset({"business_object", "bridge"})


def role_stays_pending(table_role: str | None) -> bool:
    """这个角色的对象归到兜底板块时，会不会落在「待归类业务对象」里。"""
    return (table_role or "") in _ROLES_STAYING_PENDING


def classify_fallback_kind(obj: ObjectType) -> str:
    """这个没归属的对象该进哪个兜底板块。顺序即优先级。

    先判 system 再判角色：系统表几乎都会同时被判成 technical，但「压根不该进本体」
    比「是技术表」更能说明该怎么处置它。
    """
    if is_system_table(obj.source_ref):
        return SEGMENT_KIND_SYSTEM
    if getattr(obj, "is_hub", False):
        return SEGMENT_KIND_SHARED
    if not role_stays_pending(obj.table_role):
        return SEGMENT_KIND_TECHNICAL
    return SEGMENT_KIND_PENDING


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


def place_unsegmented(db: Session, ontology_id: str) -> dict[str, int]:
    """把该本体里所有没有板块归属的对象放进兜底板块，返回各 kind 的落位计数。

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

    buckets: dict[str, list[ObjectType]] = {}
    for obj in orphans:
        buckets.setdefault(classify_fallback_kind(obj), []).append(obj)

    placed: dict[str, int] = {}
    for kind in FALLBACK_KIND_ORDER:
        members = buckets.get(kind)
        if not members:
            continue
        segment = ensure_fallback_segment(db, ontology_id, kind)
        for obj in members:
            obj.segment_id = segment.id
        placed[kind] = len(members)

    db.flush()
    for kind in placed:
        segment = ensure_fallback_segment(db, ontology_id, kind)
        segment.member_count = (
            db.query(ObjectType)
            .filter(
                ObjectType.ontology_id == ontology_id,
                ObjectType.segment_id == segment.id,
                ObjectType.deleted_by_user == False,  # noqa: E712
            )
            .count()
        )
    logger.info("补齐板块归属：%s", placed)
    return placed


def segment_kind_of(db: Session, segment_id: str | None) -> str | None:
    """板块的种类；未归属或板块已不存在都返回 ``None``。"""
    if not segment_id:
        return None
    segment = db.get(OntologySegment, segment_id)
    return segment.kind if segment else None


def needs_classification(db: Session, obj: ObjectType) -> bool:
    """这个对象是否还压在「待归类业务对象」里。

    审核的完成条件不止「角色判得对不对」，还有「归到哪个业务模块」——判成业务对象
    却连不成簇的表落在 pending 兜底板块里，光确认角色不会让它出现在任何业务地图上，
    也不会被任何板块视图看见。所以 pending 板块里的对象**不算判完**（见 edit 服务的
    复核门禁）：要么给它选一个业务板块，要么改判成数据表/技术表。
    """
    return segment_kind_of(db, obj.segment_id) == SEGMENT_KIND_PENDING


def resettle_fallback_member(db: Session, obj: ObjectType) -> str | None:
    """判定时把仍在兜底板块里的对象挪到与当前角色相符的那一个。

    只动**兜底板块**里的对象：业务板块的归属是聚类或人工的判断，改个角色不该把它
    踢出业务模块。返回新板块 id（没挪动返回 ``None``）。

    这条是「待归类必须归类」的退路：改判成数据表/技术表的对象会落到「技术表」板块，
    从而不再卡在待归类门禁上；反过来，技术表改判成业务对象会落进「待归类业务对象」，
    于是必须再被归位一次——这正是想要的，改判成业务对象本身并不是一个完整的判定。

    不要求角色**变了**才调用：存量里有一批「先落进待归类、后来被重判成技术表却没跟着
    挪」的对象（实测 erpnext 待归类板块里 6 张 technical）。它们的角色已经是对的，
    再点一次「改判技术表」是空操作，于是永远卡在门禁上。判定发生时顺手自愈，
    比让人手动挪板块可靠。
    """
    current = segment_kind_of(db, obj.segment_id)
    if current is None or current not in FALLBACK_KIND_ORDER:
        return None
    target = classify_fallback_kind(obj)
    if target == current:
        return None
    segment = ensure_fallback_segment(db, obj.ontology_id, target)
    obj.segment_id = segment.id
    return segment.id
