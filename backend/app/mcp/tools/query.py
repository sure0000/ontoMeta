"""
查询工具集

只读工具，用于查询本体、对象、关系等信息。
"""
from sqlalchemy.orm import Session, joinedload

from ...database import get_db
from ...models.ontology import Ontology, OntologyStatus
from . import register_tool, ToolResult, AuthContext


@register_tool
class QueryOntologyTool:
    """查询本体列表"""

    name = "query_ontology"
    description = "查询本体结构和业务对象列表。可以查询所有本体，或按 ID 查询特定本体。"
    input_schema = {
        "type": "object",
        "properties": {
            "ontology_id": {
                "type": "string",
                "description": "本体 ID（留空查询所有本体）",
            },
            "include_unpublished": {
                "type": "boolean",
                "description": "是否包含未发布的本体",
                "default": False,
            },
        },
    }

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        """执行查询"""
        try:
            # 获取数据库会话
            db: Session = next(get_db())

            ontology_id = arguments.get("ontology_id")
            include_unpublished = arguments.get("include_unpublished", False)

            try:
                # 查询本体（需要 join domain_context 获取名称）
                query = db.query(Ontology).options(joinedload(Ontology.domain_context))

                if ontology_id:
                    query = query.filter(Ontology.id == ontology_id)

                if not include_unpublished:
                    query = query.filter(Ontology.status == OntologyStatus.PUBLISHED.value)

                ontologies = query.all()

                # 格式化返回数据
                data = {
                    "ontologies": [
                        {
                            "id": o.id,
                            "domain_name": o.domain_context.name if o.domain_context else "Unknown",
                            "domain_id": o.domain_context_id,
                            "version": o.version,
                            "draft_revision": o.draft_revision,
                            "status": o.status,
                            "published": o.status == OntologyStatus.PUBLISHED.value,
                            "published_at": (
                                o.published_at.isoformat() if o.published_at else None
                            ),
                            "created_at": (
                                o.created_at.isoformat() if o.created_at else None
                            ),
                            "updated_at": (
                                o.updated_at.isoformat() if o.updated_at else None
                            ),
                            "description": o.domain_context.description if o.domain_context else None,
                        }
                        for o in ontologies
                    ]
                }

                return ToolResult(
                    success=True,
                    data=data,
                    metadata={
                        "count": len(ontologies),
                        "include_unpublished": include_unpublished,
                        "query_type": "single" if ontology_id else "all",
                    },
                )

            finally:
                db.close()

        except Exception as e:
            return ToolResult(success=False, error=f"Database error: {str(e)}")
