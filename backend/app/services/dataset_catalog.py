"""数据集目录：本体实体在数仓里的**可选物理句柄**。

「本体同步到数仓之后没有新本体出现，于是后续操作指不到那张表」——缺的不是本体，是
句柄。ODS/DWD 表是既有实体的物理投影（见 ``object_landing``），登记一直在写，只是没有
一个能被**选中**的名字：任务表单、Data Agent、数据应用都只列得出实体名，列不出「这个
实体落在数仓的哪张表上」。

本模块给每个已登记的落点一个稳定引用：

    obj:<object_type_id>@ods       同步落点（源表的一比一镜像）
    obj:<object_type_id>@serving   物化/清洗落点（层看 ``layer``：dim/dwd/dws/…）
    logic:<business_logic_id>@ads  口径落点（指标/标签/规则的 ADS 表）

**引用指槽位而不是层**是刻意的：一条 Projection 行就带一个 ODS 落点和一个服务层落点，
引用对应存储槽位，故它永远解析得到同一个东西；把层名写进引用（``@dwd``）的话，契约把层
从 dwd 改成 dws 就会让已经存下来的引用指空——而这些引用是要进任务 Spec 的。

**只读、零新表。** 目录完全由 ``object_landing`` 的读模型拼出来，不是第三个权威源，因此
不存在与接入契约/Projection 对不齐的可能；新增实体也不必来这里登记。同理，「这张表现在
能不能用」只由 ``derive_landing_state`` 判定，目录不自带第二套阈值。

**目录不是数仓的全集**：它只列本体认领过的落点。数仓里那些无主的表（历史遗留、别处建的、
对象被人工删除后留下的）不在这里——给它们一个可选名字等于默认它们已被治理。认领入口是
另一件事（见 ``scripts/check_landing_orphans``）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.models import (
    BusinessLogic,
    IngestionContract,
    ObjectType,
    WarehouseLogicProjection,
    WarehouseObjectProjection,
)
from app.services.object_landing import (
    LANDED,
    SCHEMA_READY,
    LogicLanding,
    ObjectLanding,
    bulk_logic_landings,
    bulk_object_landings,
    derive_landing_state,
    qualified_table,
)

OBJECT_PREFIX = "obj:"
LOGIC_PREFIX = "logic:"

SLOT_ODS = "ods"
SLOT_SERVING = "serving"
SLOT_ADS = "ads"

KIND_OBJECT = "object_type"
KIND_LOGIC = "business_logic"

# 可作为下游作业**数据来源**的状态：表已经建出来了。``schema_ready`` 表已建但没数——
# 仍算可选，因为「建了空表」是合法的中间态，拦在这里只会让人以为流程坏了；有没有数由
# 任务自检（materialize_preflight）按本次 Spec 判，那里才知道这次要不要求有数。
SOURCE_READY_STATES = frozenset({LANDED, SCHEMA_READY})

# 浏览顺序：人是按层看数仓的。未知层排在最后而不是当成某一层。
_LAYER_ORDER = {"ods": 0, "dim": 1, "dwd": 2, "dws": 3, "ads": 4}


@dataclass(frozen=True)
class DatasetEntry:
    """数仓里一张**已被本体认领**的物理表。"""

    ref: str
    entity_kind: str
    entity_id: str
    entity_name: str
    entity_display_name: str
    slot: str
    layer: str
    physical: str  # 库.表
    state: str
    queryable: bool
    mode: str | None = None  # 仅 ODS：full / incremental / cdc
    last_success_at: datetime | None = None

    @property
    def source_ready(self) -> bool:
        """能不能作为下游作业的源表。"""
        return self.state in SOURCE_READY_STATES


def dataset_ref(kind: str, entity_id: str, slot: str) -> str:
    prefix = OBJECT_PREFIX if kind == KIND_OBJECT else LOGIC_PREFIX
    return f"{prefix}{entity_id}@{slot}"


def parse_dataset_ref(ref: str) -> tuple[str, str, str] | None:
    """``obj:<id>@ods`` → ``(kind, entity_id, slot)``；形态不对返回 ``None``。

    解析失败返回 ``None`` 而不是抛异常：引用会从 Spec、前端表单、模型输出等处进来，
    调用方本来就要处理「这个引用不认识」，让它们各自 try/except 只会散落一地。
    """
    text = (ref or "").strip()
    if "@" not in text:
        return None
    head, slot = text.rsplit("@", 1)
    if head.startswith(OBJECT_PREFIX) and slot in {SLOT_ODS, SLOT_SERVING}:
        return KIND_OBJECT, head[len(OBJECT_PREFIX) :], slot
    if head.startswith(LOGIC_PREFIX) and slot == SLOT_ADS:
        return KIND_LOGIC, head[len(LOGIC_PREFIX) :], slot
    return None


def _object_entries(
    *, entity_id: str, name: str, display_name: str, landing: ObjectLanding
) -> list[DatasetEntry]:
    """一个对象的落点 → 至多两条目录项（ODS 槽 + 服务层槽）。"""
    entries: list[DatasetEntry] = []
    serving_layer = landing.serving_layer or SLOT_SERVING
    # 服务层被显式设成 ods 时两个槽指向同一张表；同一张表在选择器里出现两次是 bug，
    # 保留服务层那条——只有它带得出 queryable。
    same_table = bool(
        landing.ods_table
        and landing.serving_table
        and landing.ods_table.strip().lower() == landing.serving_table.strip().lower()
    )

    if landing.ods_table and not same_table:
        entries.append(
            DatasetEntry(
                ref=dataset_ref(KIND_OBJECT, entity_id, SLOT_ODS),
                entity_kind=KIND_OBJECT,
                entity_id=entity_id,
                entity_name=name,
                entity_display_name=display_name or name,
                slot=SLOT_ODS,
                layer=SLOT_ODS,
                physical=landing.ods_table,
                # 只喂 ODS 那一路状态：下游清洗要判的是「源表好了没」，
                # 不该被这个对象自己的加工失败染红。
                state=derive_landing_state(
                    ods_status=landing.ods_status,
                    serving_status=None,
                    schema_status=None,
                    has_target=True,
                ),
                # ODS 不对外服务（见 ingestion_contract.mirror_contract_to_projection）。
                queryable=False,
                mode=landing.ods_mode,
                last_success_at=landing.last_success_at,
            )
        )
    if landing.serving_table:
        entries.append(
            DatasetEntry(
                ref=dataset_ref(KIND_OBJECT, entity_id, SLOT_SERVING),
                entity_kind=KIND_OBJECT,
                entity_id=entity_id,
                entity_name=name,
                entity_display_name=display_name or name,
                slot=SLOT_SERVING,
                layer=serving_layer,
                physical=landing.serving_table,
                state=derive_landing_state(
                    ods_status=None,
                    serving_status=landing.serving_status,
                    schema_status=landing.schema_status,
                    has_target=True,
                ),
                queryable=landing.queryable,
                last_success_at=landing.last_success_at,
            )
        )
    return entries


def _logic_entry(
    *, entity_id: str, name: str, display_name: str, landing: LogicLanding
) -> DatasetEntry | None:
    if not landing.serving_table:
        return None
    return DatasetEntry(
        ref=dataset_ref(KIND_LOGIC, entity_id, SLOT_ADS),
        entity_kind=KIND_LOGIC,
        entity_id=entity_id,
        entity_name=name,
        entity_display_name=display_name or name,
        slot=SLOT_ADS,
        layer=SLOT_ADS,
        physical=landing.serving_table,
        state=landing.state,
        queryable=landing.queryable,
        last_success_at=landing.last_success_at,
    )


def _sort_key(entry: DatasetEntry) -> tuple[int, str, str]:
    return (
        _LAYER_ORDER.get(entry.layer, 9),
        (entry.entity_display_name or entry.entity_name).lower(),
        entry.physical.lower(),
    )


def list_datasets(
    db: Session,
    ontology_id: str,
    *,
    layer: str | None = None,
    q: str | None = None,
    source_ready_only: bool = False,
    queryable_only: bool = False,
) -> list[DatasetEntry]:
    """本体在数仓里的数据集目录。

    软删除（``deleted_by_user``）的实体不列：人已经说了这个概念不算数，再把它的表摆进
    选择器等于请人往一个被废弃的概念上继续接活。那张表若还在库里，它就是无主表，该走
    认领而不是从这里选。
    """
    object_rows = (
        db.query(ObjectType.id, ObjectType.name, ObjectType.display_name)
        .filter(
            ObjectType.ontology_id == ontology_id,
            ObjectType.deleted_by_user.is_(False),
        )
        .all()
    )
    logic_rows = (
        db.query(BusinessLogic.id, BusinessLogic.name, BusinessLogic.display_name)
        .filter(
            BusinessLogic.ontology_id == ontology_id,
            BusinessLogic.deleted_by_user.is_(False),
        )
        .all()
    )

    entries: list[DatasetEntry] = []
    landings = bulk_object_landings(db, [row[0] for row in object_rows])
    for entity_id, name, display_name in object_rows:
        landing = landings.get(entity_id)
        if landing is not None:
            entries.extend(
                _object_entries(
                    entity_id=entity_id,
                    name=name,
                    display_name=display_name,
                    landing=landing,
                )
            )
    logic_landings = bulk_logic_landings(db, [row[0] for row in logic_rows])
    for entity_id, name, display_name in logic_rows:
        landing = logic_landings.get(entity_id)
        if landing is not None:
            entry = _logic_entry(
                entity_id=entity_id,
                name=name,
                display_name=display_name,
                landing=landing,
            )
            if entry is not None:
                entries.append(entry)

    if layer:
        wanted = layer.strip().lower()
        entries = [e for e in entries if e.layer.lower() == wanted]
    if source_ready_only:
        entries = [e for e in entries if e.source_ready]
    if queryable_only:
        entries = [e for e in entries if e.queryable]
    if q:
        needle = q.strip().lower()
        entries = [
            e
            for e in entries
            if needle in e.physical.lower()
            or needle in (e.entity_display_name or "").lower()
            or needle in (e.entity_name or "").lower()
        ]
    return sorted(entries, key=_sort_key)


def claimed_physical_tables(db: Session) -> set[str]:
    """**全库范围**已被本体认领的物理表（归一小写的 ``库.表``）。

    跨本体取并集是刻意的：一张表被别的域认领了，对这个域也不是「无主」——各算各的，
    两个域会各自认领同一张表，然后各自往里写。

    只认**主人还在**的登记：实体被人工删除（软删）或硬删后留下的登记行不算数，那张表
    此刻确实没有主人，正该出现在无主表清单里由人重新认领（这就是
    ``scripts/check_landing_orphans --reattach`` 的人工版）。
    """
    names: set[str] = set()
    live_objects = {
        row[0]
        for row in db.query(ObjectType.id)
        .filter(ObjectType.deleted_by_user.is_(False))
        .all()
    }
    live_logics = {
        row[0]
        for row in db.query(BusinessLogic.id)
        .filter(BusinessLogic.deleted_by_user.is_(False))
        .all()
    }
    for contract in db.query(IngestionContract).all():
        if contract.object_type_id in live_objects:
            names.add(_norm_table(contract.target_ods_database, contract.target_ods_table))
    for projection in db.query(WarehouseObjectProjection).all():
        if projection.object_type_id in live_objects:
            names.add(_norm_table(projection.ods_database, projection.ods_table))
            names.add(_norm_table(projection.serving_database, projection.serving_table))
    for projection in db.query(WarehouseLogicProjection).all():
        if projection.business_logic_id in live_logics:
            names.add(_norm_table(projection.serving_database, projection.serving_table))
    names.discard("")
    return names


def _norm_table(database: str | None, table: str | None) -> str:
    qualified = qualified_table(database, table)
    return qualified.strip().lower() if qualified else ""


def resolve_dataset_ref(db: Session, ref: str) -> DatasetEntry | None:
    """引用 → 目录项。指向不存在/未登记的实体时返回 ``None``。

    解析走的是与 ``list_datasets`` 同一套构造，所以「选的时候看到的」与「跑的时候解析到的」
    是同一张表——两边各拼一次表名正是要消除的那类分叉。
    """
    parsed = parse_dataset_ref(ref)
    if parsed is None:
        return None
    kind, entity_id, slot = parsed
    if kind == KIND_OBJECT:
        obj = db.get(ObjectType, entity_id)
        # 软删除的实体不在目录里（见 list_datasets），解析也必须给同一个答案：两处
        # 判定不一致的话，一个引用会「选不到却解析得出」，于是存量任务继续指着一张
        # 已经无主的表跑下去。
        if obj is None or obj.deleted_by_user:
            return None
        landing = bulk_object_landings(db, [obj.id]).get(obj.id)
        if landing is None:
            return None
        for entry in _object_entries(
            entity_id=obj.id,
            name=obj.name,
            display_name=obj.display_name,
            landing=landing,
        ):
            if entry.slot == slot:
                return entry
        return None
    logic = db.get(BusinessLogic, entity_id)
    if logic is None or logic.deleted_by_user:
        return None
    landing = bulk_logic_landings(db, [logic.id]).get(logic.id)
    if landing is None:
        return None
    return _logic_entry(
        entity_id=logic.id,
        name=logic.name,
        display_name=logic.display_name,
        landing=landing,
    )


__all__ = [
    "KIND_LOGIC",
    "KIND_OBJECT",
    "SLOT_ADS",
    "SLOT_ODS",
    "SLOT_SERVING",
    "SOURCE_READY_STATES",
    "DatasetEntry",
    "claimed_physical_tables",
    "dataset_ref",
    "list_datasets",
    "parse_dataset_ref",
    "resolve_dataset_ref",
]
