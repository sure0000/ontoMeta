"""已发布本体的只读投影：给 SQL 语义证明器喂的查询友好快照。

Agent 写的 SQL 用的是**本体标识符**（ObjectType.name / Property.name），执行前才由
``data_app_executor._apply_mapping`` 翻译成物理表列名。故本投影按**本体 name**索引，
证明器直接拿 SQL 里的表/列 token 对它解析，无需反解物理名。

只投影 ``status=published`` 的对象/属性/关系——这正是封闭世界假设（CWA）的那个
「世界」：凡不在此投影中的表/列/关系，一律视为不存在，据此拒答。

一次构建、多次校验（Agent 一轮问答可能连发多条 SQL）。不可变、无副作用。

设计见 FORMAL_VALIDATION_IMPL.md 第二部分 §2.2。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models import EntityStatus, ObjectType, Property, RelationType
from app.ontology_types import Cardinality, SemanticType, normalize_cardinality, normalize_semantic_type


@dataclass(frozen=True)
class PropView:
    name: str
    object_name: str
    semantic_type: SemanticType
    data_type: str | None


@dataclass(frozen=True)
class ObjView:
    id: str
    name: str
    display_name: str
    props: dict[str, PropView] = field(default_factory=dict)  # 归一小写 prop_name -> PropView

    def resolve_property(self, col: str) -> PropView | None:
        return self.props.get((col or "").strip().lower())


@dataclass(frozen=True)
class RelView:
    id: str
    name: str
    src_obj: str  # 源对象 name
    tgt_obj: str  # 目标对象 name
    cardinality: Cardinality | None
    structure_type: str | None


@dataclass(frozen=True)
class OntologyProjection:
    objects: dict[str, ObjView]  # 归一小写 obj_name -> ObjView
    relations_by_pair: dict[frozenset[str], list[RelView]]  # {src,tgt}(小写) -> 关系列表
    mapping_tables: dict[str, str]  # 本体 name -> 物理表（来自 DataSource.mapping_json）
    mapping_columns: dict[str, str]

    def object_of(self, table_token: str) -> ObjView | None:
        """按 SQL 里的表 token 解析已发布对象（本体 name，大小写不敏感）。"""
        return self.objects.get((table_token or "").strip().lower())

    def relation_between(self, a: str, b: str) -> list[RelView]:
        """两个对象 name 之间已声明的业务关系（无向）。无则空列表。"""
        return self.relations_by_pair.get(
            frozenset({(a or "").strip().lower(), (b or "").strip().lower()}), []
        )

    def has_physical_mapping(self, obj: ObjView) -> bool:
        """该对象是否配了物理表映射（决定 SQL 能否真正执行；语义证明不强依赖它）。"""
        return obj.name in self.mapping_tables


def build_projection(
    db: Session, ontology_id: str, mapping: dict | None = None
) -> OntologyProjection:
    """构建已发布本体投影。

    ``mapping`` 为目标数据源的 ``mapping_json``（``{"tables":{...},"columns":{...}}``），
    可为 None（此时仅做语义证明、不校验物理可执行性）。
    """
    published = EntityStatus.PUBLISHED.value

    objects_raw = (
        db.query(ObjectType)
        .filter(
            ObjectType.ontology_id == ontology_id,
            ObjectType.status == published,
        )
        .all()
    )
    obj_by_id: dict[str, ObjView] = {}
    objects: dict[str, ObjView] = {}
    for o in objects_raw:
        view = ObjView(id=o.id, name=o.name, display_name=o.display_name, props={})
        objects[o.name.strip().lower()] = view
        obj_by_id[o.id] = view

    # 属性：只取已发布对象下的已发布属性
    if obj_by_id:
        props_raw = (
            db.query(Property)
            .filter(
                Property.object_type_id.in_(list(obj_by_id.keys())),
                Property.status == published,
            )
            .all()
        )
        for p in props_raw:
            owner = obj_by_id.get(p.object_type_id)
            if owner is None:
                continue
            owner.props[p.name.strip().lower()] = PropView(
                name=p.name,
                object_name=owner.name,
                semantic_type=normalize_semantic_type(p.semantic_type),
                data_type=p.data_type,
            )

    # 关系：两端都在已发布对象集里才纳入（与发布口径一致）
    relations_by_pair: dict[frozenset[str], list[RelView]] = {}
    relations_raw = (
        db.query(RelationType)
        .filter(
            RelationType.ontology_id == ontology_id,
            RelationType.status == published,
        )
        .all()
    )
    for r in relations_raw:
        src = obj_by_id.get(r.source_object_type_id)
        tgt = obj_by_id.get(r.target_object_type_id)
        if src is None or tgt is None:
            continue
        rel = RelView(
            id=r.id,
            name=r.name,
            src_obj=src.name,
            tgt_obj=tgt.name,
            cardinality=normalize_cardinality(r.cardinality),
            structure_type=r.structure_type,
        )
        key = frozenset({src.name.strip().lower(), tgt.name.strip().lower()})
        relations_by_pair.setdefault(key, []).append(rel)

    mapping = mapping or {}
    return OntologyProjection(
        objects=objects,
        relations_by_pair=relations_by_pair,
        mapping_tables=dict(mapping.get("tables") or {}),
        mapping_columns=dict(mapping.get("columns") or {}),
    )


__all__ = [
    "PropView",
    "ObjView",
    "RelView",
    "OntologyProjection",
    "build_projection",
]
