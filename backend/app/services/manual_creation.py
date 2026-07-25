"""人工生成（manual generation）服务。

与「从 DataHub 预生成本体草稿」互补的另一条建模入口：

- **DataHub 预生成**：面向**历史/存量数据**的本体治理——从已有元数据（表、字段、
  外键、血缘）逆向抽取业务对象与关系，是对既有数据资产做「事后」本体化。
- **人工生成（本模块）**：面向**新业务**的「本体先行」建模——业务人员先用本体
  思想手工定义业务对象与关系，再据此在选定数据源上创建对应的物理表，让新业务
  从第一天起就以本体方式管理数据（forward / ontology-first）。

本模块负责：在数据域的草稿本体中手工写入业务对象（及其属性），并按选定数据源
方言生成建表 DDL（供评审/执行）。物理表的实际执行依赖数据源连接配置，作为受控
的下一步（见 DOMAIN_MODEL.md「两种本体生成方式」）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models import DomainContext, ObjectType, Ontology, Property
from app.models.ontology import EntityStatus, OntologyStatus

# 语义类型 → 各数据源方言的物理列类型映射。仅覆盖常见方言，未知方言退回 ANSI。
_TYPE_BY_DIALECT: dict[str, dict[str, str]] = {
    "mysql": {
        "identifier": "BIGINT",
        "amount": "DECIMAL(18,2)",
        "datetime": "DATETIME",
        "category": "VARCHAR(128)",
        "flag": "TINYINT(1)",
        "attribute": "VARCHAR(255)",
        "technical": "VARCHAR(255)",
        "text": "TEXT",
    },
    "postgresql": {
        "identifier": "BIGINT",
        "amount": "NUMERIC(18,2)",
        "datetime": "TIMESTAMP",
        "category": "VARCHAR(128)",
        "flag": "BOOLEAN",
        "attribute": "VARCHAR(255)",
        "technical": "VARCHAR(255)",
        "text": "TEXT",
    },
    "hive": {
        "identifier": "BIGINT",
        "amount": "DECIMAL(18,2)",
        "datetime": "TIMESTAMP",
        "category": "STRING",
        "flag": "BOOLEAN",
        "attribute": "STRING",
        "technical": "STRING",
        "text": "STRING",
    },
    "sqlite": {
        "identifier": "INTEGER",
        "amount": "REAL",
        "datetime": "TEXT",
        "category": "TEXT",
        "flag": "INTEGER",
        "attribute": "TEXT",
        "technical": "TEXT",
        "text": "TEXT",
    },
}

_ANSI_TYPES = {
    "identifier": "BIGINT",
    "amount": "DECIMAL(18,2)",
    "datetime": "TIMESTAMP",
    "category": "VARCHAR(128)",
    "flag": "SMALLINT",
    "attribute": "VARCHAR(255)",
    "technical": "VARCHAR(255)",
    "text": "VARCHAR(1024)",
}

SUPPORTED_DIALECTS = tuple(_TYPE_BY_DIALECT.keys())


@dataclass
class ManualPropertyInput:
    name: str
    display_name: str | None = None
    data_type: str | None = None
    semantic_type: str | None = None
    required: bool = False
    primary_key: bool = False


@dataclass
class ManualObjectResult:
    ontology_id: str
    object_type_id: str
    table_name: str
    ddl: str


def _snake(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "_", (name or "").strip()).strip("_").lower()


def _column_type(dialect: str, semantic_type: str | None, data_type: str | None) -> str:
    """优先使用用户显式填写的物理类型，否则按语义类型映射到方言列类型。"""
    if data_type and data_type.strip():
        return data_type.strip()
    table = _TYPE_BY_DIALECT.get((dialect or "").lower(), _ANSI_TYPES)
    return table.get((semantic_type or "attribute"), table.get("attribute", "VARCHAR(255)"))


def generate_create_table_ddl(
    table_name: str,
    properties: list[ManualPropertyInput],
    *,
    dialect: str = "mysql",
    comment: str | None = None,
) -> str:
    """根据业务对象属性生成 CREATE TABLE DDL（按数据源方言映射列类型）。

    - 主键：标记 primary_key 的列组成主键；无标记时不设主键（新业务可后续补充）。
    - 列类型：优先取用户填写的 data_type，否则由 semantic_type 映射。
    纯字符串生成、不触库，安全可测；实际执行由数据源连接层在受控前提下完成。
    """
    tbl = _snake(table_name) or "new_table"
    cols: list[str] = []
    pk_cols: list[str] = []
    for prop in properties:
        col = _snake(prop.name)
        if not col:
            continue
        col_type = _column_type(dialect, prop.semantic_type, prop.data_type)
        parts = [f"  {col} {col_type}"]
        if prop.required or prop.primary_key:
            parts.append("NOT NULL")
        cols.append(" ".join(parts))
        if prop.primary_key:
            pk_cols.append(col)
    if pk_cols:
        cols.append(f"  PRIMARY KEY ({', '.join(pk_cols)})")
    body = ",\n".join(cols) if cols else "  id BIGINT"
    ddl = f"CREATE TABLE {tbl} (\n{body}\n)"
    if comment and (dialect or "").lower() in ("mysql",):
        safe = comment.replace("'", "''")
        ddl += f" COMMENT='{safe}'"
    return ddl + ";"


class ManualCreationService:
    """把「人工定义的业务对象」写入数据域的草稿本体，并产出建表 DDL。"""

    def _get_or_create_draft_ontology(self, db: Session, domain: DomainContext) -> Ontology:
        draft = (
            db.query(Ontology)
            .filter(
                Ontology.domain_context_id == domain.id,
                Ontology.status == OntologyStatus.DRAFT.value,
            )
            .order_by(Ontology.updated_at.desc())
            .first()
        )
        if draft:
            return draft
        draft = Ontology(
            domain_context_id=domain.id,
            status=OntologyStatus.DRAFT.value,
            generated_by="manual",
        )
        db.add(draft)
        db.flush()
        return draft

    def create_object(
        self,
        db: Session,
        domain_id: str,
        *,
        name: str,
        display_name: str,
        description: str | None,
        properties: list[ManualPropertyInput],
        dialect: str = "mysql",
        data_source: str | None = None,
    ) -> ManualObjectResult:
        domain = db.get(DomainContext, domain_id)
        if not domain:
            raise ValueError("数据域不存在")
        ident = _snake(name)
        if not ident:
            raise ValueError("对象标识名不能为空")

        ontology = self._get_or_create_draft_ontology(db, domain)

        obj = ObjectType(
            ontology_id=ontology.id,
            name=ident,
            display_name=display_name.strip() or ident,
            description=(description or None),
            table_role="business_object",
            status=EntityStatus.EDITED.value,
            origin="user",
            user_created=True,
            source_ref=f"manual:{data_source or dialect}:{ident}",
        )
        db.add(obj)
        db.flush()

        for prop in properties:
            pident = _snake(prop.name)
            if not pident:
                continue
            db.add(
                Property(
                    object_type_id=obj.id,
                    name=pident,
                    display_name=(prop.display_name or prop.name).strip(),
                    data_type=prop.data_type,
                    semantic_type=prop.semantic_type,
                    required=bool(prop.required or prop.primary_key),
                    status=EntityStatus.EDITED.value,
                    origin="user",
                    user_created=True,
                )
            )

        ddl = generate_create_table_ddl(
            ident, properties, dialect=dialect, comment=display_name or None
        )
        db.commit()
        return ManualObjectResult(
            ontology_id=ontology.id,
            object_type_id=obj.id,
            table_name=_snake(ident),
            ddl=ddl,
        )
