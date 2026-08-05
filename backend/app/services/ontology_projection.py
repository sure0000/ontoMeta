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
    display_name: str
    src_obj: str  # 源对象 name
    tgt_obj: str  # 目标对象 name
    cardinality: Cardinality | None
    structure_type: str | None
    # JOIN 键（本体属性名）。推不出时为 None——此时只能说「有关系」，不能给 ON。
    src_key: str | None = None
    tgt_key: str | None = None
    # 桥接对象 name：N:N 关系必经桥表，直连两端是错的。
    bridge_obj: str | None = None


# ---------------------------------------------------------------------------
# JOIN 键推断规则（与 warehouse_generator 共用同一份实现，避免两处规则漂移）
#
# 语义类型的判定**留给调用方**：物化侧按原始字符串精确匹配 "identifier"，
# 投影侧按 normalize_semantic_type 归一（含 id/key/pk/fk/code 别名）。
# 这里只固化「命名约定」本身，不替调用方决定谁算标识字段。
# ---------------------------------------------------------------------------

DEFAULT_REF_COLUMN = "id"


def primary_key_name(
    obj_name: str, prop_names, identifier_names
) -> str | None:
    """身份属性：优先 ``<对象名>_id``，其次首个标识语义字段（字典序，保证确定性）。"""
    if f"{obj_name}_id" in set(prop_names):
        return f"{obj_name}_id"
    identifiers = sorted(identifier_names)
    return identifiers[0] if identifiers else None


def foreign_key_names(
    evidence: dict | None, *, target_name: str, target_pk: str | None
) -> tuple[str, str]:
    """外键关系的 (源端列, 目标端列)。

    取值优先级沿用 ``connectors/cube.py`` 的既有约定：
    源端 ``foreign_key`` → ``source_field`` → ``fk_column`` → ``<目标对象>_id``；
    目标端 ``target_field`` → ``pk_column`` → 目标对象身份属性 → ``id``。
    """
    ev = evidence or {}
    fk = (
        ev.get("foreign_key")
        or ev.get("source_field")
        or ev.get("fk_column")
        or f"{target_name}_id"
    )
    ref = (
        ev.get("target_field")
        or ev.get("pk_column")
        or target_pk
        or DEFAULT_REF_COLUMN
    )
    return str(fk), str(ref)


def other_is_many(r: RelView, m: ObjView) -> bool | None:
    """从对象 ``m`` 看，关系 ``r`` 的另一端是否为「多」端。未知基数返回 None。

    ``RelView.cardinality`` 存的是 src→tgt 方向，故以 m 为「本端」时要换算。
    扇出判定（SQL 证明器）与路径安全性判定（语义导航器）**必须**用同一份换算，
    否则会出现「导航器说这条路安全、证明器却拒了」的自相矛盾。
    """
    card = r.cardinality
    if card is None:
        return None
    if m.name == r.src_obj:
        # 本端=src：ONE_TO_MANY → 另一端(tgt)是多；MANY_TO_ONE → 另一端是一
        return card in (Cardinality.ONE_TO_MANY, Cardinality.MANY_TO_MANY)
    if m.name == r.tgt_obj:
        # 本端=tgt：MANY_TO_ONE(src多→tgt一) 反看即 tgt一→src多 → 另一端是多
        return card in (Cardinality.MANY_TO_ONE, Cardinality.MANY_TO_MANY)
    return None


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

    def partners_of(self, obj_name: str) -> list[str]:
        """与该对象有已声明关系的对端对象 name（去重、有序）。

        供拒绝提示回答「那它能和谁 JOIN」——只说「A 与 B 无关系」模型无从修正。
        自反关系（两端同一对象）会落在单元素 key 上，此时对端即自身。
        """
        key = (obj_name or "").strip().lower()
        out: list[str] = []
        for pair, rels in self.relations_by_pair.items():
            if key not in pair:
                continue
            for other in pair - {key} or {key}:
                view = self.objects.get(other)
                name = view.name if view is not None else other
                if name not in out:
                    out.append(name)
        return sorted(out)

    def has_physical_mapping(self, obj: ObjView) -> bool:
        """该对象是否配了物理表映射（决定 SQL 能否真正执行；语义证明不强依赖它）。"""
        return obj.name in self.mapping_tables


def _identity_of(obj: ObjView) -> str | None:
    """对象的身份属性（投影侧按归一语义类型判定标识字段）。"""
    return primary_key_name(
        obj.name,
        [pv.name for pv in obj.props.values()],
        [
            pv.name
            for pv in obj.props.values()
            if pv.semantic_type is SemanticType.IDENTIFIER
        ],
    )


def _join_keys_of(
    rel: RelationType, src: ObjView, tgt: ObjView
) -> tuple[str | None, str | None]:
    """推断一条关系两端的 JOIN 列，并**对投影校验存在性**。

    推出来的列名若不是该对象的已发布属性，一律回 None——宁可说「有关系但给不出 ON」，
    也不能吐一个 SQL 证明器随后会以 ``unknown_column`` 拒掉的 ON，那只会让模型空转。
    """
    import json

    try:
        evidence = json.loads(rel.source_evidence) if rel.source_evidence else {}
    except (TypeError, ValueError):
        evidence = {}
    if not isinstance(evidence, dict):
        evidence = {}

    src_key, tgt_key = foreign_key_names(
        evidence, target_name=tgt.name, target_pk=_identity_of(tgt)
    )
    return (
        src_key if src.resolve_property(src_key) else None,
        tgt_key if tgt.resolve_property(tgt_key) else None,
    )


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
        src_key, tgt_key = _join_keys_of(r, src, tgt)
        bridge = obj_by_id.get(r.mapping_object_type_id or "")
        rel = RelView(
            id=r.id,
            name=r.name,
            display_name=r.display_name,
            src_obj=src.name,
            tgt_obj=tgt.name,
            cardinality=normalize_cardinality(r.cardinality),
            structure_type=r.structure_type,
            src_key=src_key,
            tgt_key=tgt_key,
            bridge_obj=bridge.name if bridge is not None else None,
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
    "primary_key_name",
    "foreign_key_names",
    "other_is_many",
    "DEFAULT_REF_COLUMN",
]
