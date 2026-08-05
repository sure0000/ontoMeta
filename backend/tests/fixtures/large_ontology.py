"""大本体 fixture：把「734 对象 / 4113 关系」那种真实规模搬进测试。

**为什么需要它**：DATA_AGENT_V2_PLAN 里好几条判断都只在大本体上成立——
6 步预算不够用、检索结果污染主上下文、子 agent 隔离才有价值。
golden set 用的是 2 对象的小域，这些判断在那儿一条都验证不了，
于是 P4.2 一直没法做（做了也证明不了有用）。

本 fixture 生成一个**结构真实**的域：多个业务板块、板块内密集、板块间稀疏、
少量高连通枢纽对象（公司/文档类型这类到处被引用的公共维度）——
这正是 `community_detection` 与 `find_join_path` 会遇到的形状。

确定性：同样的参数必然产出同样的本体（无随机数），基线才可对照。
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models import (
    BusinessLogic,
    DomainContext,
    EntityStatus,
    ObjectType,
    Ontology,
    OntologyStatus,
    Property,
    RelationType,
)

PUB = EntityStatus.PUBLISHED.value

# 业务板块：(标识前缀, 中文名, 该板块的对象主题)
_SEGMENTS: list[tuple[str, str, list[str]]] = [
    ("sales", "销售", ["订单", "订单明细", "报价", "合同", "退货"]),
    ("crm", "客户", ["客户", "联系人", "商机", "拜访记录", "客户分层"]),
    ("inv", "库存", ["物料", "仓库", "库存流水", "盘点单", "批次"]),
    ("fin", "财务", ["应收单", "应付单", "凭证", "科目", "结算单"]),
    ("logi", "物流", ["发货单", "承运商", "运单", "签收记录", "线路"]),
    ("hr", "人力", ["员工", "部门", "岗位", "考勤", "薪资单"]),
]

# 到处被引用的公共维度（枢纽）：真实 ERP 里的「公司」「文档类型」就是这种
_HUBS: list[tuple[str, str]] = [
    ("company", "公司"),
    ("doc_type", "文档类型"),
    ("currency", "币种"),
]

# 每个对象的字段模板：(后缀, 中文名, 语义类型, 数据类型)
_FIELDS: list[tuple[str, str, str, str]] = [
    ("id", "主键", "identifier", "bigint"),
    ("code", "编号", "identifier", "varchar"),
    ("name", "名称", "textual", "varchar"),
    ("status", "状态", "categorical", "varchar"),
    ("amount", "金额", "measure", "decimal"),
    ("qty", "数量", "measure", "decimal"),
    ("created_at", "创建时间", "temporal", "datetime"),
    ("remark", "备注", "textual", "varchar"),
]


@dataclass
class LargeOntology:
    domain_id: str
    ontology_id: str
    object_count: int
    relation_count: int
    property_count: int
    logic_count: int
    # 板块名 -> 该板块对象的标识符（供用例挑「相关/无关」实体做对照）
    segments: dict[str, list[str]] = field(default_factory=dict)
    hub_names: list[str] = field(default_factory=list)


def seed_large_ontology(
    db: Session, *, objects_per_segment: int = 12, name_suffix: str = ""
) -> LargeOntology:
    """生成并**发布**一个大本体。

    ``objects_per_segment=12`` → 6 板块 × 12 + 3 枢纽 = 75 对象、~600 字段。
    调到 60 即 363 对象，接近真实 ERP 域的量级；测试默认取小值保证跑得快，
    需要压规模的用例自行调大。
    """
    domain = DomainContext(
        datahub_domain_id=f"urn:li:domain:large{name_suffix}",
        name=f"大规模域{name_suffix}",
        description="大本体 fixture",
    )
    db.add(domain)
    db.flush()
    onto = Ontology(
        domain_context_id=domain.id, status=OntologyStatus.PUBLISHED.value, version=1
    )
    db.add(onto)
    db.flush()

    objects: dict[str, ObjectType] = {}
    segments: dict[str, list[str]] = {}

    def _add_object(name: str, display: str, role: str = "business_object") -> ObjectType:
        o = ObjectType(
            ontology_id=onto.id, name=name, display_name=display,
            description=f"{display}（{name}）", table_role=role, status=PUB,
        )
        db.add(o)
        objects[name] = o
        return o

    for prefix, seg_cn, topics in _SEGMENTS:
        names: list[str] = []
        for i in range(objects_per_segment):
            topic = topics[i % len(topics)]
            idx = i // len(topics)
            name = f"{prefix}_{topic_pinyin(topic)}_{i:03d}"
            display = f"{seg_cn}·{topic}{idx + 1 if idx else ''}"
            _add_object(name, display)
            names.append(name)
        segments[seg_cn] = names
    for hub_name, hub_cn in _HUBS:
        _add_object(hub_name, hub_cn, role="data_table")
    db.flush()

    # 关系先定型：板块内链式密集 + 每个对象挂一个枢纽 + 板块间稀疏通路
    hub_objs = [objects[h] for h, _ in _HUBS]
    edges: list[tuple[ObjectType, ObjectType, str]] = []
    for seg_cn, names in segments.items():
        for i in range(len(names) - 1):
            edges.append((objects[names[i]], objects[names[i + 1]], f"{seg_cn}链{i}"))
    # 板块首对象串成链：跨板块通路，多跳寻路要用
    seg_heads = [objects[names[0]] for names in segments.values()]
    for i in range(len(seg_heads) - 1):
        edges.append((seg_heads[i], seg_heads[i + 1], f"跨板块{i}"))
    for name, obj in objects.items():
        if any(obj.id == h.id for h in hub_objs):
            continue
        # 用 crc32 而非内置 hash：后者对字符串**逐进程随机**，会让 fixture 每次跑出
        # 不同的枢纽分配，基线就不可对照了
        hub = hub_objs[zlib.crc32(name.encode()) % len(hub_objs)]
        edges.append((obj, hub, "归属"))

    # 字段：通用字段 + **每条关系在源对象上对应的外键列**。
    # 少了外键列，`find_join_path` 推不出 ON——真实 ERP 对象是有 `<目标>_id` 的，
    # 没有就测不出多跳寻路，而那正是大本体才有的场景。
    props: list[Property] = []
    for name, obj in objects.items():
        for suffix, cn, sem, dt in _FIELDS:
            props.append(
                Property(
                    object_type_id=obj.id,
                    name=f"{name}_id" if suffix == "id" else f"{name}_{suffix}",
                    display_name=cn, semantic_type=sem, data_type=dt, status=PUB,
                )
            )
    fk_seen: set[tuple[str, str]] = set()
    for src, tgt, _label in edges:
        key = (src.id, f"{tgt.name}_id")
        if key in fk_seen:
            continue
        fk_seen.add(key)
        props.append(
            Property(
                object_type_id=src.id, name=f"{tgt.name}_id",
                display_name=f"{tgt.display_name}ID", semantic_type="identifier",
                data_type="bigint", status=PUB,
            )
        )
    db.bulk_save_objects(props)

    relations = [_fk(onto.id, src, tgt, label) for src, tgt, label in edges]
    db.bulk_save_objects(relations)

    logics = [
        BusinessLogic(
            ontology_id=onto.id, name=f"{prefix}_total", display_name=f"{seg_cn}总额",
            logic_type="metric", expression_summary=f"SUM({seg_cn}金额)", status=PUB,
        )
        for prefix, seg_cn, _ in _SEGMENTS
    ]
    db.bulk_save_objects(logics)
    db.commit()

    return LargeOntology(
        domain_id=domain.id,
        ontology_id=onto.id,
        object_count=len(objects),
        relation_count=len(relations),
        property_count=len(props),
        logic_count=len(logics),
        segments=segments,
        hub_names=[cn for _, cn in _HUBS],
    )


def _fk(onto_id: str, src: ObjectType, tgt: ObjectType, label: str) -> RelationType:
    return RelationType(
        ontology_id=onto_id,
        name=f"rel_{src.name}__{tgt.name}",
        display_name=f"{src.display_name}{label}{tgt.display_name}",
        source_object_type_id=src.id,
        target_object_type_id=tgt.id,
        cardinality="many_to_one",
        structure_type="foreign_key",
        status=PUB,
    )


_PINYIN = {
    "订单": "order", "订单明细": "order_line", "报价": "quote", "合同": "contract",
    "退货": "return", "客户": "customer", "联系人": "contact", "商机": "opportunity",
    "拜访记录": "visit", "客户分层": "tier", "物料": "material", "仓库": "warehouse",
    "库存流水": "stock_move", "盘点单": "stocktake", "批次": "batch",
    "应收单": "receivable", "应付单": "payable", "凭证": "voucher", "科目": "account",
    "结算单": "settlement", "发货单": "delivery", "承运商": "carrier", "运单": "waybill",
    "签收记录": "signoff", "线路": "route", "员工": "employee", "部门": "department",
    "岗位": "position", "考勤": "attendance", "薪资单": "payroll",
}


def topic_pinyin(topic: str) -> str:
    return _PINYIN.get(topic, "obj")


__all__ = ["LargeOntology", "seed_large_ontology"]
