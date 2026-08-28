"""未建模表清单：数据域里还没进本体的那几张表。

这是「按表增量建模」的入口——先看清楚到底哪几张表是新的，再只对它们跑 LLM，
而不是为了几张新表把整个域重扫一遍（ERP 域 734 张表，一次几十万 token，还会把
``needs_review`` 重新灌满、把部分发布的门闸打回去）。

**只列清单，不自动生成。** 触发权在人手里：机器判断「没建模」靠的是 ``source_ref``
对不上，而对不上有多种成因（真新表 / 上游改了标识 / 人工删过），哪一种该建模只有人
知道。自动跑一遍的代价正是这套方案要消除的东西。

**排除项**有三类，都不是「待建模」：

1. 已在本体里的表（``ObjectType.source_ref`` 命中）。
2. 人工删除过的对象所对应的表——机器不会复活人工删掉的对象（见 ``ontology_merge``），
   列出来只会让人点了没反应。
3. 平台自己造出来的表（ODS/服务层落点）。它们是既有对象的物理投影，不是新的业务
   概念；真被采集进同一个域时也必须挡掉，否则又会生成一份重复对象——这正是本方案
   要根除的那类重复。见 ``services/object_landing``。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.connectors.datahub import _extract_dataset_name
from app.models import DomainContext, ObjectType
from app.schemas import DataHubDomainBundle
from app.services import draft_evidence_cache, ontology_workspace
from app.services.evidence_builder import EvidenceBuilder
from app.services.object_landing import bulk_object_landings
from app.services.settings_service import SettingsService

logger = logging.getLogger("ontometa.unmodeled_tables")


@dataclass(frozen=True)
class UnmodeledTable:
    urn: str
    name: str
    display_name: str | None
    description: str | None
    platform: str | None
    field_count: int
    row_count: int | None


def _landing_table_names(db: Session, ontology_id: str) -> set[str]:
    """本体各对象已登记的物理落点表名（归一小写的 ``库.表``）。"""
    object_ids = [
        row[0]
        for row in db.query(ObjectType.id)
        .filter(ObjectType.ontology_id == ontology_id)
        .all()
    ]
    names: set[str] = set()
    for landing in bulk_object_landings(db, object_ids).values():
        for table in (landing.ods_table, landing.serving_table):
            if table:
                names.add(table.strip().lower())
    return names


async def fetch_domain_bundle(db: Session, domain: DomainContext) -> DataHubDomainBundle:
    """实时拉取该域的 DataHub 元数据。**不走证据缓存**——清单要的就是最新状态。"""
    from app.connectors.datahub import DataHubConnector

    runtime = SettingsService().get_datahub_runtime(db)
    connector = DataHubConnector(runtime)
    try:
        return await connector.fetch_domain_bundle(
            domain.datahub_domain_id, include_logic_evidences=False
        )
    finally:
        await connector.aclose()


async def list_unmodeled_tables(
    db: Session, domain_id: str
) -> tuple[list[UnmodeledTable], int]:
    """返回 (未建模表清单, 域内表总数)。

    抓取顺带回填证据缓存：紧接着的「只生成选中的表」因此不必再等一次分钟级抓取，
    且它拿到的就是本清单所依据的同一份元数据——「我选的和我生成的是同一批」这件事
    是靠共用这份缓存保证的，不是靠时间上凑巧。
    """
    domain = db.get(DomainContext, domain_id)
    if domain is None:
        raise ValueError("数据域不存在")

    bundle = await fetch_domain_bundle(db, domain)
    evidence = EvidenceBuilder().build(bundle, include_business_logics=False)
    draft_evidence_cache.save(domain_id, domain.datahub_domain_id or "none", evidence)

    ontology = ontology_workspace.get_working_ontology(db, domain_id)
    modeled: set[str] = set()
    excluded_tables: set[str] = set()
    if ontology is not None:
        for source_ref, deleted in (
            db.query(ObjectType.source_ref, ObjectType.deleted_by_user)
            .filter(
                ObjectType.ontology_id == ontology.id,
                ObjectType.source_ref.isnot(None),
            )
            .all()
        ):
            # 人工删除过的也算「已处理」：机器不会复活它，列出来点了不会有反应。
            modeled.add(source_ref)
            if deleted:
                logger.debug("表 %s 曾被人工删除，不列入未建模清单", source_ref)
        excluded_tables = _landing_table_names(db, ontology.id)

    unmodeled: list[UnmodeledTable] = []
    for dataset in bundle.datasets:
        if not dataset.urn or dataset.urn in modeled:
            continue
        table_name = (_extract_dataset_name(dataset.urn) or dataset.name or "").strip()
        if table_name.lower() in excluded_tables:
            # 平台自己建的落点表：它是既有对象的投影，不是新的业务概念。
            continue
        unmodeled.append(
            UnmodeledTable(
                urn=dataset.urn,
                name=table_name or dataset.name,
                display_name=dataset.display_name,
                description=dataset.description,
                platform=dataset.platform,
                field_count=len(dataset.fields),
                row_count=dataset.row_count,
            )
        )

    unmodeled.sort(key=lambda t: (t.display_name or t.name or "").lower())
    logger.info(
        "数据域 %s 未建模表 %d / 域内共 %d",
        domain_id,
        len(unmodeled),
        len(bundle.datasets),
    )
    return unmodeled, len(bundle.datasets)


__all__ = ["UnmodeledTable", "list_unmodeled_tables"]
