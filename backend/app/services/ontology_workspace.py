"""一域一本体：数据域工作本体的唯一入口。

见 ``docs/ONTOLOGY_LIFECYCLE_REDESIGN.md``。全系统里「这个数据域的本体是哪一行」
只有这一个答案：域内恒定一行，它既是草稿工作台也是发布载体。

历史上生成/人工建模都按 ``status == draft`` 找工作台，而 ``publish()`` 会把那一行
**就地**翻成 published——于是发布后再生成必然落空并新建一个空白本体行：三方合并的
``machine_baseline`` / ``overridden_fields`` 全空，上一版的人工改名、改角色、复核确认
在新行里根本不存在，机器结论原样复活；再发布一次，域内还会攒出两个 published 本体，
本体浏览与 Agent 可检索集一起翻倍。按 domain 取行、不看 status，从根上消除那条分叉。

配套前提：``publish()`` 必须把已发布实体的结构性字段钉住
（``ontology_merge.seed_published_authority``），否则两行并一行后，再生成会直接
静默改写正在对外服务的内容。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import Ontology, OntologyStatus

logger = logging.getLogger("ontometa.ontology_workspace")

_EPOCH = datetime(1970, 1, 1)


def _naive(value: datetime | None) -> datetime:
    """统一成 naive 便于排序：库里既有 server_default 的 naive 值，也有代码写入的
    tz-aware 值（``datetime.now(timezone.utc)``），直接比较会 TypeError。"""
    if value is None:
        return _EPOCH
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def list_domain_ontologies(db: Session, domain_id: str) -> list[Ontology]:
    return (
        db.query(Ontology).filter(Ontology.domain_context_id == domain_id).all()
    )


def get_working_ontology(db: Session, domain_id: str) -> Ontology | None:
    """返回该域的工作本体；没有则 None。**不看 status**。

    唯一约束落库前仍可能查到多行（历史分叉）。选行规则与迁移
    ``4aa435f23621`` 保持一致：已发布行优先——人工权威与对外服务都在它身上；多个已
    发布时取 version 最大者，同版本取 **created_at 最早** 的那行（分叉总是更新的，原始
    血脉在旧行）；没有已发布行时取最新创建的草稿行。
    """
    rows = list_domain_ontologies(db, domain_id)
    if not rows:
        return None
    published = [o for o in rows if o.status == OntologyStatus.PUBLISHED.value]
    if published:
        return sorted(
            published, key=lambda o: (-(o.version or 0), _naive(o.created_at))
        )[0]
    return max(rows, key=lambda o: _naive(o.created_at))


def get_or_create_working_ontology(
    db: Session, domain_id: str, *, generated_by: str = "llm"
) -> Ontology:
    """取该域的工作本体，没有就建一行草稿。调用方负责 commit。"""
    ontology = get_working_ontology(db, domain_id)
    if ontology is not None:
        return ontology
    ontology = Ontology(
        domain_context_id=domain_id,
        status=OntologyStatus.DRAFT.value,
        generated_by=generated_by,
    )
    db.add(ontology)
    db.flush()
    logger.info("为数据域 %s 新建工作本体 %s", domain_id, ontology.id)
    return ontology


def list_stale_ontologies(db: Session, domain_id: str) -> list[Ontology]:
    """域内除工作本体之外的多余本体行（历史分叉产物）。"""
    working = get_working_ontology(db, domain_id)
    if working is None:
        return []
    return [o for o in list_domain_ontologies(db, domain_id) if o.id != working.id]


def discard_unpublished(db: Session, domain_id: str) -> dict[str, int]:
    """丢弃工作本体里**从未发布过的**实体，回到「只剩已发布内容」的状态。

    一域一本体后，「草稿卡住、既不能删又挡住别的路」这个死锁本身已经不存在了
    （没有第二行可挡）。这里补的是另一个真实诉求：一次生成结果不理想，想把没发布过
    的部分清掉重来，而不牵动已经对外承诺的内容。

    只删 ``status != published`` 的对象/关系及其附属；已发布实体连同人工对它们的
    改动一概不动——那是既成的对外承诺，不该被一个「丢弃草稿」按钮顺手带走。
    """
    from app.models import (
        BusinessLogicObjectBinding,
        BusinessLogicPropertyBinding,
        EntityStatus,
        ObjectType,
        Property,
        RelationType,
    )

    working = get_working_ontology(db, domain_id)
    if working is None:
        raise ValueError("该数据域尚无本体")

    published = EntityStatus.PUBLISHED.value
    objects = (
        db.query(ObjectType)
        .filter(
            ObjectType.ontology_id == working.id,
            ObjectType.status != published,
        )
        .all()
    )
    object_ids = [o.id for o in objects]

    # 关系：自身未发布，或任一端点将被删除（悬空关系不能留）。
    doomed_relations = set()
    for rel in db.query(RelationType).filter(
        RelationType.ontology_id == working.id
    ):
        if (
            rel.status != published
            or rel.source_object_type_id in set(object_ids)
            or rel.target_object_type_id in set(object_ids)
        ):
            doomed_relations.add(rel.id)

    property_ids: list[str] = []
    if object_ids:
        property_ids = [
            p.id
            for p in db.query(Property).filter(
                Property.object_type_id.in_(object_ids)
            )
        ]

    if property_ids:
        db.query(BusinessLogicPropertyBinding).filter(
            BusinessLogicPropertyBinding.property_id.in_(property_ids)
        ).delete(synchronize_session=False)
    if object_ids:
        db.query(BusinessLogicObjectBinding).filter(
            BusinessLogicObjectBinding.object_type_id.in_(object_ids)
        ).delete(synchronize_session=False)
    if doomed_relations:
        db.query(RelationType).filter(
            RelationType.id.in_(list(doomed_relations))
        ).delete(synchronize_session=False)
    if property_ids:
        db.query(Property).filter(Property.id.in_(property_ids)).delete(
            synchronize_session=False
        )
    if object_ids:
        db.query(ObjectType).filter(ObjectType.id.in_(object_ids)).delete(
            synchronize_session=False
        )
    db.commit()
    logger.info(
        "丢弃未发布内容 domain=%s objects=%d relations=%d properties=%d",
        domain_id,
        len(object_ids),
        len(doomed_relations),
        len(property_ids),
    )
    return {
        "object_types": len(object_ids),
        "relation_types": len(doomed_relations),
        "properties": len(property_ids),
    }
