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

import json
import re
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


#: 强到可以据以发强制约束的身份命名约定。除此之外都只是「像个 id 的列」。
#: 顺序即优先级，``primary_key_name`` 与 ``primary_key_is_confident`` 共用这一份，
#: 二者**必须在说同一列**——见下面 primary_key_name 的说明。
_CONFIDENT_PK_NAMES = ("{obj}_id", "id")


def primary_key_name(
    obj_name: str, prop_names, identifier_names
) -> str | None:
    """身份属性：优先命名约定（``<对象名>_id`` → ``id``），其次首个标识语义字段。

    **``id`` 必须在"首个标识字段"之前**：两个函数曾对不上口径——``primary_key_is_confident``
    看到 ``id`` 就说"有把握"，而这里在没有 ``<对象名>_id`` 时直接跳到字典序第一个标识字段。
    真实 Odoo 的 ``sale_order`` 正是这个形状：有 ``id``（真主键），另有 20 个 ``*_id`` 外键列
    全被标成 identifier 语义。于是「有把握」的是 ``id``，返回的却是 ``analytic_account_id``
    ——一个大量为空的外键。据此建出的 ODS 表主键错位，装不进自己的源数据
    （同类事故见记忆 inferred-facts-not-enforced）。

    命名约定都没命中时才退回标识字段，且此时 ``primary_key_is_confident`` 为 False：
    语义导航/去重照用，但不据以发强制约束。
    """
    names = set(prop_names)
    for pattern in _CONFIDENT_PK_NAMES:
        candidate = pattern.format(obj=obj_name)
        if candidate in names:
            return candidate
    identifiers = sorted(identifier_names)
    return identifiers[0] if identifiers else None


def primary_key_is_confident(obj_name: str, prop_names, identifier_names) -> bool:
    """这个身份属性是**命中命名约定**，还是只是「名字里有 id」。

    只有 ``<对象名>_id`` / ``id`` 算数。**「全表只有这一个候选」不算**——候选少不等于
    它唯一：ERP 的 bank 表只有 ``custom_enterprise_seed_external_id`` 一个标识语义字段
    （一个自定义扩展字段），照样既不唯一也大量为空，建成主键后这张表一行都装不进。

    真实源零 PK 声明、也没开 profiling（unique_count/row_count 全空），所以除了命名约定
    没有别的证据可依。有了 profiling 之后，这里应改为「唯一值数 == 行数」的真证据判定。

    有把握与否只决定**要不要发强制约束**：语义导航、去重这些用途照旧使用身份属性，
    猜错顶多结果不准；而 postgres 建成真 PRIMARY KEY 后，猜错等于这张表永远装不进数据。
    """
    names = set(prop_names)
    return any(
        pattern.format(obj=obj_name) in names for pattern in _CONFIDENT_PK_NAMES
    )


def foreign_key_names(
    evidence: dict | None, *, target_name: str, target_pk: str | None
) -> tuple[str, str]:
    """外键关系的 (源端列, 目标端列)。

    取值优先级遵循本体投影的既有约定：
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


# 关系证据里「外键列」的两种历史写法。``source_evidence`` 的契约是 JSON
# （warehouse_generator 就按 JSON 读），但草稿生成实际写进去的是人话描述——
# 于是 JSON 解析失败 → 证据为空 → 外键退化成猜 ``<目标对象>_id``，而真实列叫
# ``country``/``bank_name``，猜的列在对象上不存在 → ON 一律给不出来。
# 结果：本体里明明记着「通过引用字段 X 关联 Y」，导航器却说推不出 ON。
#
# 这里不做任何**推断**：只把我们自己写进去的那个列名读回来（模板见
# evidence_builder 的 FK 证据描述、以及 evidence_refs 的 ``urn#column`` 片段）。
# 读回来的列还要过 `_join_keys_of` 的存在性校验，读错了也只会退回「给不出 ON」。
_FK_IN_PROSE = re.compile(r"引用字段\s*[`\"']?([A-Za-z_][A-Za-z0-9_]*)[`\"']?\s*关联")
_FK_IN_URN_REF = re.compile(r"#([A-Za-z_][A-Za-z0-9_]*)\s*(?:,|$)")
#: 证据里记着这条关系是哪套源画像推出来的（evidence_builder 写的「来源 X 源画像」）。
_PROFILE_IN_EVIDENCE = re.compile(r"来源\s*([A-Za-z_][A-Za-z0-9_]*)\s*源画像")
#: 源画像**声明**的主键列（不是猜的：见 source_profile.FrappeProfile.primary_key_names
#: ——「Frappe 以 name 为字符串主键」）。外键的被引用列取这里，仅在该列真实存在时生效。
_PROFILE_PK_COLUMN: dict[str, str] = {"frappe": "name"}


def parse_relation_evidence(raw: str | None) -> dict:
    """``RelationType.source_evidence`` → 结构化证据。认不出的部分不臆造。

    优先 JSON（既有契约）；否则从人话描述/证据引用里把外键列名读回来。
    """
    if not raw:
        return {}
    text = str(raw).strip()
    if not text:
        return {}
    try:
        loaded = json.loads(text)
    except (TypeError, ValueError):
        loaded = None
    if isinstance(loaded, dict):
        return loaded

    out: dict = {}
    for pattern in (_FK_IN_PROSE, _FK_IN_URN_REF):
        hit = pattern.search(text)
        if hit:
            out["foreign_key"] = hit.group(1)
            break
    profile = _PROFILE_IN_EVIDENCE.search(text)
    if profile:
        out["source_profile"] = profile.group(1).lower()
    return out


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


def _referenced_key_of(tgt: ObjView, evidence: dict) -> str | None:
    """外键**被引用**的那一列。取不到就回 None——不拿「像个 id 的列」顶替。

    与 ``primary_key_name`` 的「身份属性」不同：那个可以退回「首个标识语义字段」，
    对展示/去重够用；但 JOIN 的被引用列取错就是**连错行**。实测里 code_list 的
    身份属性被判成 publisher_id，据此拼出的 ON 是一条会算错数的连接。

    取值顺序：命名约定的强主键（``<对象>_id`` / ``id``）→ 源画像声明的主键列
    （如 Frappe 的 ``name``，见 source_profile）。两者都要求该列真实存在于已发布属性里。
    """
    names = [pv.name for pv in tgt.props.values()]
    if primary_key_is_confident(tgt.name, names, []):
        return primary_key_name(tgt.name, names, [])
    profile_pk = _PROFILE_PK_COLUMN.get(str(evidence.get("source_profile") or "").lower())
    if profile_pk and tgt.resolve_property(profile_pk):
        return profile_pk
    return None


def _join_keys_of(
    rel: RelationType, src: ObjView, tgt: ObjView
) -> tuple[str | None, str | None]:
    """推断一条关系两端的 JOIN 列，并**对投影校验存在性**。

    推出来的列名若不是该对象的已发布属性，一律回 None——宁可说「有关系但给不出 ON」，
    也不能吐一个 SQL 证明器随后会以 ``unknown_column`` 拒掉的 ON，那只会让模型空转。
    """
    evidence = parse_relation_evidence(rel.source_evidence)
    src_key, tgt_key = foreign_key_names(
        evidence, target_name=tgt.name, target_pk=_referenced_key_of(tgt, evidence)
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
    "primary_key_is_confident",
    "foreign_key_names",
    "other_is_many",
    "DEFAULT_REF_COLUMN",
]
