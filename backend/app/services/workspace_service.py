import logging

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import (
    DomainContext,
    ObjectType,
    Ontology,
    OntologyStatus,
    RelationType,
)
from app.schemas import DomainContextDetail, DomainContextSummary
from app.services.draft_task_service import DraftTaskService
from app.services.logic_query import OntologyQueryService

logger = logging.getLogger("ontometa.workspace")


class WorkspaceService(DraftTaskService):
    """工作区：数据域同步与草稿生成。"""

    async def sync_domains(self, db: Session) -> list[DomainContextSummary]:
        connector = self._datahub(db)
        try:
            domains = await connector.list_domains()
        except Exception:
            logger.warning("无法连接 DataHub 同步数据域，将返回本地缓存数据", exc_info=True)
            return self.list_domains(db)
        finally:
            await connector.aclose()
        for domain in domains:
            existing = (
                db.query(DomainContext)
                .filter(DomainContext.datahub_domain_id == domain.id)
                .first()
            )
            if existing:
                existing.name = domain.name
                existing.description = domain.description
                existing.owner = domain.owner
            else:
                db.add(
                    DomainContext(
                        datahub_domain_id=domain.id,
                        name=domain.name,
                        description=domain.description,
                        owner=domain.owner,
                    )
                )
        db.commit()
        return self.list_domains(db)

    def list_domains(self, db: Session) -> list[DomainContextSummary]:
        domains = db.query(DomainContext).order_by(DomainContext.updated_at.desc()).all()
        if not domains:
            return []
        domain_ids = [d.id for d in domains]

        draft_statuses = [OntologyStatus.DRAFT.value, OntologyStatus.IN_REVIEW.value]

        # 一次性聚合 draft / published 数量
        draft_rows = (
            db.query(
                Ontology.domain_context_id,
                func.count(Ontology.id),
                func.max(Ontology.updated_at),
            )
            .filter(
                Ontology.domain_context_id.in_(domain_ids),
                Ontology.status.in_(draft_statuses),
            )
            .group_by(Ontology.domain_context_id)
            .all()
        )
        published_rows = (
            db.query(
                Ontology.domain_context_id,
                func.count(Ontology.id),
                func.max(Ontology.published_at),
            )
            .filter(
                Ontology.domain_context_id.in_(domain_ids),
                Ontology.status == OntologyStatus.PUBLISHED.value,
            )
            .group_by(Ontology.domain_context_id)
            .all()
        )
        draft_map = {did: (cnt, latest) for did, cnt, latest in draft_rows}
        published_map = {did: (cnt, latest) for did, cnt, latest in published_rows}

        # 每个 domain 的最新本体：单查询取所有域的本体，Python 端取最大 updated_at
        status_by_domain: dict[str, str] = {}
        latest_ontology_by_domain: dict[str, str] = {}
        status_rows = (
            db.query(
                Ontology.id,
                Ontology.domain_context_id,
                Ontology.updated_at,
                Ontology.status,
            )
            .filter(Ontology.domain_context_id.in_(domain_ids))
            .all()
        )
        best: dict[str, tuple] = {}
        for oid, did, updated_at, status in status_rows:
            prev = best.get(did)
            if prev is None or updated_at > prev[0]:
                best[did] = (updated_at, status, oid)
        status_by_domain = {did: st for did, (_, st, _) in best.items()}
        latest_ontology_by_domain = {did: oid for did, (_, _, oid) in best.items()}

        published_ontology_by_domain: dict[str, str] = {}
        published_best: dict[str, tuple] = {}
        for oid, did, published_at, version in (
            db.query(
                Ontology.id,
                Ontology.domain_context_id,
                Ontology.published_at,
                Ontology.version,
            )
            .filter(
                Ontology.domain_context_id.in_(domain_ids),
                Ontology.status == OntologyStatus.PUBLISHED.value,
            )
            .all()
        ):
            sort_key = (
                published_at.timestamp() if published_at else 0,
                version,
            )
            prev = published_best.get(did)
            if prev is None or sort_key > prev[0]:
                published_best[did] = (sort_key, oid)
        published_ontology_by_domain = {
            did: oid for did, (_, oid) in published_best.items()
        }

        latest_ontology_ids = list(latest_ontology_by_domain.values())
        published_ontology_ids = list(published_ontology_by_domain.values())
        entity_counts = OntologyQueryService()._bulk_ontology_entity_counts(
            db, latest_ontology_ids
        )
        published_entity_counts = OntologyQueryService()._bulk_ontology_entity_counts(
            db, published_ontology_ids
        )

        result: list[DomainContextSummary] = []
        for domain in domains:
            draft_count, latest_draft_at = draft_map.get(domain.id, (0, None))
            published_count, latest_published_at = published_map.get(domain.id, (0, None))
            domain_status = status_by_domain.get(domain.id, "active")
            latest_oid = latest_ontology_by_domain.get(domain.id)
            published_oid = published_ontology_by_domain.get(domain.id)
            object_count, relation_count, _ = entity_counts.get(latest_oid, (0, 0, 0)) if latest_oid else (0, 0, 0)
            published_object_count, _, _ = (
                published_entity_counts.get(published_oid, (0, 0, 0)) if published_oid else (0, 0, 0)
            )
            result.append(
                DomainContextSummary(
                    id=domain.id,
                    datahub_domain_id=domain.datahub_domain_id,
                    name=domain.name,
                    description=domain.description,
                    owner=domain.owner,
                    status=domain_status,
                    draft_count=draft_count,
                    published_count=published_count,
                    object_type_count=object_count,
                    relation_type_count=relation_count,
                    published_object_type_count=published_object_count,
                    latest_draft_at=latest_draft_at,
                    latest_published_at=latest_published_at,
                    updated_at=domain.updated_at,
                )
            )
        return result

    def get_domain(self, db: Session, domain_id: str) -> DomainContextDetail | None:
        domain = db.get(DomainContext, domain_id)
        if not domain:
            return None
        summary = next((d for d in self.list_domains(db) if d.id == domain_id), None)
        if not summary:
            return None
        latest = (
            db.query(Ontology)
            .filter(Ontology.domain_context_id == domain_id)
            .order_by(Ontology.updated_at.desc())
            .first()
        )
        published = OntologyQueryService().get_published_ontology(db, domain_id)
        datahub = self._datahub(db)
        return DomainContextDetail(
            **summary.model_dump(),
            datahub_url=datahub.get_domain_url(domain.datahub_domain_id),
            latest_ontology_id=latest.id if latest else None,
            latest_ontology_status=latest.status if latest else None,
            published_ontology_id=published.id if published else None,
            published_ontology_version=published.version if published else None,
        )
