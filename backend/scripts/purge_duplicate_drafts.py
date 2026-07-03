"""一次性脚本：清理每个数据域多余的 draft 本体，仅保留最新一条。

适用场景：在引入"重新生成草稿前清理旧 draft"逻辑之前，已生成的存量数据中
存在同一数据域多条 draft 本体，导致工作区卡片"草稿 N"数字虚高。
本脚本对每个数据域的 draft 本体按 updated_at 倒序排列，保留第 1 条，
其余通过 query._delete_ontologies_cascade 级联删除。

in_review / published / archived 状态的本体不受影响。
EntityChangeLog 通过 entity_id 字符串引用本体，保留作为审计历史。

用法：
    cd backend
    source .venv/bin/activate
    python -m scripts.purge_duplicate_drafts
"""

from app.database import SessionLocal
from app.models import DomainContext, Ontology, OntologyStatus
from app.services.query import WorkspaceService


def main() -> None:
    service = WorkspaceService()
    with SessionLocal() as db:
        domains = db.query(DomainContext).all()
        total_purged = 0
        for domain in domains:
            drafts = (
                db.query(Ontology)
                .filter(
                    Ontology.domain_context_id == domain.id,
                    Ontology.status == OntologyStatus.DRAFT.value,
                )
                .order_by(Ontology.updated_at.desc())
                .all()
            )
            if len(drafts) <= 1:
                continue
            keep = drafts[0]
            purge_ids = [o.id for o in drafts[1:]]
            n = service._delete_ontologies_cascade(db, purge_ids)
            total_purged += n
            print(
                f"{domain.name}: 保留 draft {keep.id[:8]}… "
                f"(updated_at={keep.updated_at}), 清理 {n} 条多余 draft"
            )
        db.commit()
        print(f"done. total purged: {total_purged}")


if __name__ == "__main__":
    main()
