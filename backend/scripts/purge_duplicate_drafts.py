"""一次性脚本：清理每个数据域多余的 draft 本体，仅保留最新一条。

产品化去重已纳入草稿生成流程（生成前检测 + 重新生成时自动 purge）。
本脚本仅用于存量库手工清理；日常请使用：

    GET /api/domains/{domain_id}/draft-duplicates

用法：
    cd backend
    source .venv/bin/activate
    python -m scripts.purge_duplicate_drafts
"""

from app.database import SessionLocal
from app.services.query import WorkspaceService


def main() -> None:
    service = WorkspaceService()
    with SessionLocal() as db:
        from app.models import DomainContext

        domains = db.query(DomainContext).all()
        total_purged = 0
        for domain in domains:
            report = service.report_duplicate_drafts(db, domain.id)
            if report.draft_count <= 1:
                continue
            keep_id = report.draft_ontology_ids[0]
            purge_ids = report.draft_ontology_ids[1:]
            n = service._delete_ontologies_cascade(db, purge_ids)
            total_purged += n
            print(
                f"{domain.name}: 保留 draft {keep_id[:8]}…，清理 {n} 条多余 draft"
                f"（{report.message}）"
            )
        db.commit()
        print(f"done. total purged: {total_purged}")


if __name__ == "__main__":
    main()
