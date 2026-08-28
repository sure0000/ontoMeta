"""派生建模：由数仓里的几张表 join 出一个**新粒度**的业务对象。

「同步到数仓之后没有新本体出现」的正确答案不是造一个新本体，而是分清两件事：

- 源表 → ODS、ODS → DWD 的 1:1 清洗：一行代表的东西没变（同样的粒度、同样的标识、
  同样的业务含义）→ 那是同一个实体的**另一个落点**，不建实体（见 ``object_landing``）。
- 多表 join 出的宽表、汇总表、快照表：一行代表的东西变了（从「一张订单」变成
  「订单×商品×日」）→ 那是一个**新的业务概念**，必须在本体里有名字，否则下游没有
  任何东西可引用，也就无从治理。

判据只有一条：**换了粒度才换实体，换了层只换落点。** 所以 ``grain`` 在这里是必填的——
它就是判据本身；允许留空等于允许把一个 1:1 落点包装成新对象，重复对象由此重新长出来。

新实体仍落在**同一个本体**里（一域一本体）。它与普通对象的唯一区别是 ``source_ref``
的形态（``derived:<本体 id>:<标识>``）与一份 ``DerivedDefinition``：

- **不能同步**：``has_physical_source`` 为 False，同步 Drafter 直接拒（它的上游在数仓，
  不在源库）。
- **可以物化**：物化只按本体属性建表，不看 ``source_ref``。
- **靠清洗落数**：上游/连接条件/字段来源都在定义里，供多源清洗任务读取。

**派生必须是声明，不是推断。** 上游、粒度、连接条件由人写下来；靠扫描 Doris 反推出
「新对象」正是重复对象的来源（见 ``unmodeled_tables``）。所以本模块不调 LLM、不猜。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models import (
    DerivedDefinition,
    MaterializationContract,
    ObjectType,
    Ontology,
    Property,
)
from app.models.ontology import EntityStatus
from app.models.warehouse import MaterializationLayer, TargetKind
from app.services import dataset_catalog
from app.services.dataset_catalog import DatasetEntry
from app.services.edit import _assert_object_name_free, _mark_overridden
from app.services.source_ref import DERIVED_PREFIX

# 派生对象可落的层。ODS 是贴源层，派生结果按定义不贴源；ADS 是口径的物化，那归
# BusinessLogic（口径不建对象，见 object_landing）。剩下的才是派生实体的去处。
ALLOWED_LAYERS = (
    MaterializationLayer.DWD.value,
    MaterializationLayer.DWS.value,
    MaterializationLayer.DIM.value,
)
DEFAULT_LAYER = MaterializationLayer.DWD.value

_JOIN_HOW = {"inner", "left"}


class DerivedObjectError(ValueError):
    """派生定义不成立。调用方（API）转成 400，不是 500。"""


@dataclass
class JoinCondition:
    left: str
    right: str


@dataclass
class UpstreamJoin:
    """把某个上游接到已在图里的另一个上游上。"""

    left_ref: str
    right_ref: str
    on: list[JoinCondition]
    how: str = "inner"


@dataclass
class FieldSource:
    """派生对象的一个属性来自哪个上游的哪一列。"""

    property: str
    from_ref: str
    from_column: str
    display_name: str | None = None


@dataclass
class DerivedObjectInput:
    name: str
    display_name: str
    grain: str
    upstream_refs: list[str]
    fields: list[FieldSource]
    description: str | None = None
    joins: list[UpstreamJoin] = field(default_factory=list)
    layer: str = DEFAULT_LAYER
    notes: str | None = None


@dataclass(frozen=True)
class DerivedObjectResult:
    object_type_id: str
    name: str
    display_name: str
    ontology_id: str
    layer: str
    upstream_refs: list[str]


@dataclass(frozen=True)
class DerivedDefinitionView:
    """读模型：定义 + 上游此刻的落点状态（上游可能已被删/未搬完）。"""

    object_type_id: str
    grain: str
    layer: str | None
    upstreams: list[DatasetEntry]
    dangling_refs: list[str]
    joins: list[dict]
    field_mapping: list[dict]
    notes: str | None


def _snake(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "_", (name or "").strip()).strip("_").lower()


def _resolve_upstreams(
    db: Session, ontology_id: str, refs: list[str]
) -> list[DatasetEntry]:
    """把上游引用解析成目录项，并挡住三类不成立的上游。

    只要求引用**能解析**（落点已登记），不要求已经搬完数：建模可以先于落数发生，
    在这里卡「必须有数」会逼人为了建模先去跑一遍任务。数据就绪与否由任务自检按本次
    Spec 判（见 materialize_preflight）。
    """
    if not refs:
        raise DerivedObjectError("派生对象至少要有一个上游数据集")
    if len(set(refs)) != len(refs):
        raise DerivedObjectError("上游数据集重复，请去重后再提交")

    # 直接拿本体的目录做白名单，而不是逐个 resolve：这样「能选的」与「能用的」是同一份
    # 清单，且天然挡住跨本体上游——跨域 join 物理上做得到，但那个新概念同时属于两个域，
    # 该落在哪个域的本体里没有答案。
    catalog = {entry.ref: entry for entry in dataset_catalog.list_datasets(db, ontology_id)}
    missing = [ref for ref in refs if ref not in catalog]
    if missing:
        raise DerivedObjectError(
            "这些上游不在当前本体的数仓落点里（可能已被删除、尚未登记，或属于别的本体）："
            + "、".join(missing)
        )
    return [catalog[ref] for ref in refs]


def _validate_joins(
    joins: list[UpstreamJoin], refs: list[str]
) -> list[dict]:
    """连接条件必须把每个上游都接进来，且只接到已在图里的上游上。

    少一条 join 就是一次笛卡尔积——它不会报错，只会安静地把行数乘起来，等到有人核对
    数量时已经过去很久了。故这里要求连通，而不是「尽力而为」。
    """
    if len(refs) == 1:
        return []
    known = {refs[0]}
    pending = {j.right_ref: j for j in joins}
    ordered: list[dict] = []
    for ref in refs[1:]:
        join = pending.get(ref)
        if join is None:
            raise DerivedObjectError(f"上游「{ref}」缺少连接条件，会产生笛卡尔积")
        if join.left_ref not in known:
            raise DerivedObjectError(
                f"连接条件的左侧「{join.left_ref}」还没有接进来，请调整上游顺序"
            )
        if not join.on:
            raise DerivedObjectError(f"上游「{ref}」的连接条件为空，会产生笛卡尔积")
        how = (join.how or "inner").lower()
        if how not in _JOIN_HOW:
            raise DerivedObjectError(f"不支持的连接方式「{join.how}」")
        ordered.append(
            {
                "left_ref": join.left_ref,
                "right_ref": ref,
                "how": how,
                "on": [{"left": c.left, "right": c.right} for c in join.on],
            }
        )
        known.add(ref)
    return ordered


def _upstream_properties(db: Session, entries: list[DatasetEntry]) -> dict[str, dict[str, Property]]:
    """上游引用 → {列名: 上游属性}。

    列取自上游**对象的属性**而不是去数仓 information_schema 读：ODS 是源表的一比一
    镜像，DWD 落点的列也由同一个对象的属性生成，所以本体里的属性就是那张表的列，
    而且带着语义类型——照抄下来，派生对象的字段类型就不必再猜一遍。
    """
    object_ids = [
        e.entity_id for e in entries if e.entity_kind == dataset_catalog.KIND_OBJECT
    ]
    props: dict[str, dict[str, Property]] = {}
    if not object_ids:
        return props
    rows = (
        db.query(Property)
        .filter(Property.object_type_id.in_(object_ids), Property.deleted_by_user.is_(False))
        .all()
    )
    by_object: dict[str, dict[str, Property]] = {}
    for row in rows:
        by_object.setdefault(row.object_type_id, {})[row.name.lower()] = row
    for entry in entries:
        props[entry.ref] = by_object.get(entry.entity_id, {})
    return props


def _validate_fields(
    fields: list[FieldSource],
    refs: list[str],
    upstream_props: dict[str, dict[str, Property]],
) -> list[tuple[FieldSource, Property | None]]:
    if not fields:
        raise DerivedObjectError("派生对象至少要有一个字段")
    seen: set[str] = set()
    resolved: list[tuple[FieldSource, Property | None]] = []
    for item in fields:
        ident = _snake(item.property)
        if not ident:
            raise DerivedObjectError("字段标识名不能为空")
        if ident in seen:
            raise DerivedObjectError(f"字段「{ident}」重复")
        seen.add(ident)
        if item.from_ref not in refs:
            raise DerivedObjectError(
                f"字段「{ident}」的来源「{item.from_ref}」不在上游列表里"
            )
        source = (upstream_props.get(item.from_ref) or {}).get(
            (item.from_column or "").lower()
        )
        # 上游列在本体里找不到时不拦：上游可能是口径的 ADS 表（列不由对象属性定义）。
        # 但类型就无从照抄，落成 attribute 由人在字段页改——比编一个类型强。
        resolved.append((FieldSource(
            property=ident,
            from_ref=item.from_ref,
            from_column=item.from_column,
            display_name=item.display_name,
        ), source))
    return resolved


def create_derived_object(
    db: Session, ontology_id: str, payload: DerivedObjectInput
) -> DerivedObjectResult:
    """在本体里新建一个派生对象。调用方无需再 commit。"""
    ontology = db.get(Ontology, ontology_id)
    if ontology is None:
        raise DerivedObjectError("本体不存在")

    ident = _snake(payload.name)
    if not ident:
        raise DerivedObjectError("对象标识名不能为空")
    display_name = (payload.display_name or "").strip()
    if not display_name:
        raise DerivedObjectError("对象中文名不能为空")
    grain = (payload.grain or "").strip()
    if not grain:
        # 粒度就是「该不该是新实体」的判据；没有它，这个接口就退化成了「随便建个对象」。
        raise DerivedObjectError(
            "必须声明粒度（这张表的一行代表什么）——粒度没变就不该建新对象，"
            "而应把它登记为已有对象的落点"
        )
    layer = (payload.layer or DEFAULT_LAYER).lower()
    if layer not in ALLOWED_LAYERS:
        raise DerivedObjectError(
            f"派生对象不能落在「{layer}」层；可选：{'、'.join(ALLOWED_LAYERS)}"
        )

    _assert_object_name_free(db, ontology_id, ident)
    entries = _resolve_upstreams(db, ontology_id, payload.upstream_refs)
    joins = _validate_joins(payload.joins, payload.upstream_refs)
    upstream_props = _upstream_properties(db, entries)
    fields = _validate_fields(payload.fields, payload.upstream_refs, upstream_props)

    obj = ObjectType(
        ontology_id=ontology_id,
        name=ident,
        display_name=display_name,
        description=(payload.description or None),
        table_role="business_object",
        status=EntityStatus.EDITED.value,
        origin="user",
        # 人建的对象机器不删不改（ontology_merge），派生对象尤其不能被下一次全量重扫
        # 抹掉——它在数仓里可能已经有表有数了。
        user_created=True,
        # 人显式声明的东西不该再要人复核一遍；needs_review=True 还会把它挡在发布之外。
        needs_review=False,
        source_ref=f"{DERIVED_PREFIX}{ontology_id}:{ident}",
    )
    db.add(obj)
    db.flush()

    for item, source in fields:
        db.add(
            Property(
                object_type_id=obj.id,
                name=item.property,
                display_name=(
                    item.display_name
                    or (source.display_name if source else None)
                    or item.property
                ),
                description=(source.description if source else None),
                data_type=(source.data_type if source else None),
                semantic_type=(source.semantic_type if source else None),
                required=bool(source.required) if source else False,
                status=EntityStatus.EDITED.value,
                origin="user",
                user_created=True,
            )
        )

    db.add(
        DerivedDefinition(
            ontology_id=ontology_id,
            object_type_id=obj.id,
            grain=grain,
            upstream_refs_json=json.dumps(payload.upstream_refs, ensure_ascii=False),
            joins_json=json.dumps(joins, ensure_ascii=False) if joins else None,
            field_mapping_json=json.dumps(
                [
                    {
                        "property": item.property,
                        "from_ref": item.from_ref,
                        "from_column": item.from_column,
                    }
                    for item, _source in fields
                ],
                ensure_ascii=False,
            ),
            notes=(payload.notes or None),
        )
    )
    _pin_layer(db, ontology_id, obj.id, layer)
    db.commit()
    return DerivedObjectResult(
        object_type_id=obj.id,
        name=ident,
        display_name=display_name,
        ontology_id=ontology_id,
        layer=layer,
        upstream_refs=list(payload.upstream_refs),
    )


def _pin_layer(db: Session, ontology_id: str, object_type_id: str, layer: str) -> None:
    """把选定的层写进物化契约并**标记为人工钉住**。

    不能只写值：契约的机器推导按 ``table_role`` 给层（business_object → dim），下一次
    推导会把这里选的 dwd 改回去。钉住是既有机制（``_mark_overridden``），派生对象照用，
    不为它另开一条「派生对象特殊对待」的分支——那种分支迟早和推导规则分叉。
    """
    contract = (
        db.query(MaterializationContract)
        .filter(
            MaterializationContract.ontology_id == ontology_id,
            MaterializationContract.target_kind == TargetKind.OBJECT_TYPE.value,
            MaterializationContract.target_id == object_type_id,
        )
        .first()
    )
    if contract is None:
        contract = MaterializationContract(
            ontology_id=ontology_id,
            target_kind=TargetKind.OBJECT_TYPE.value,
            target_id=object_type_id,
            materialized=True,
            derivation_reason="派生对象：由数据集按声明的粒度加工而来",
        )
        db.add(contract)
    contract.target_layer = layer
    # 列表参数不是笔误：``_mark_overridden`` 收字段名列表，传字符串会把它拆成一串
    # 单字符字段名钉住。origin 由它自己设，不在这里另写一份。
    _mark_overridden(contract, ["target_layer"])


def get_definition(db: Session, object_type_id: str) -> DerivedDefinitionView | None:
    """派生定义 + 上游此刻的落点。非派生对象返回 ``None``。"""
    row = (
        db.query(DerivedDefinition)
        .filter(DerivedDefinition.object_type_id == object_type_id)
        .first()
    )
    if row is None:
        return None
    refs = _loads(row.upstream_refs_json, [])
    upstreams: list[DatasetEntry] = []
    dangling: list[str] = []
    for ref in refs:
        entry = dataset_catalog.resolve_dataset_ref(db, str(ref))
        # 上游可能被删/被降级，此时**要显示出来**而不是悄悄少列一个：少一个上游的
        # 派生定义看起来仍然成立，跑起来才发现少了一张表。
        if entry is None:
            dangling.append(str(ref))
        else:
            upstreams.append(entry)
    layer = (
        db.query(MaterializationContract.target_layer)
        .filter(
            MaterializationContract.target_kind == TargetKind.OBJECT_TYPE.value,
            MaterializationContract.target_id == object_type_id,
        )
        .scalar()
    )
    return DerivedDefinitionView(
        object_type_id=object_type_id,
        grain=row.grain,
        layer=layer,
        upstreams=upstreams,
        dangling_refs=dangling,
        joins=_loads(row.joins_json, []),
        field_mapping=_loads(row.field_mapping_json, []),
        notes=row.notes,
    )


def _loads(raw: str | None, default):
    if not raw:
        return default
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


__all__ = [
    "ALLOWED_LAYERS",
    "DEFAULT_LAYER",
    "DerivedDefinitionView",
    "DerivedObjectError",
    "DerivedObjectInput",
    "DerivedObjectResult",
    "FieldSource",
    "JoinCondition",
    "UpstreamJoin",
    "create_derived_object",
    "get_definition",
]
