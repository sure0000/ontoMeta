"""桥表(关系表)塌缩：把一张 bridge 表所引用的业务对象选出两个端点，
使它塌缩成一条 `BO_A —(经桥表)→ BO_B` 的业务关系（桥表作 mapping 实现表）。

背景与动机见 draft_consistency 的 ``bridge_object_not_materialized`` 校验：
每个 table_role=bridge 的对象必须落地为某条 RelationType 的 ``mapping_object``，
否则发布时被静默丢弃、它所代表的业务关联随之消失。手工端点
``EditService.convert_object_to_relation`` 已实现单表转换；这里提供**确定性**
的端点选择，供生成流水线（evidence_builder）与存量 backfill 脚本共用，把该
转换自动化。

本模块只做「给定桥表引用的一组目标对象 → 选哪两个作端点」这一纯决策，不触库、
不依赖 ORM/证据类型，便于单测与复用。目标对象名一律用与 ``ObjectType.candidate_name``
一致的推断对象名（由调用方预先归一化后传入）。
"""

from __future__ import annotations

ROLE_BUSINESS_OBJECT = "business_object"


def select_bridge_endpoints(
    ref_targets: list[str],
    role_of: dict[str, str],
    degree: dict[str, int] | None = None,
    row_count: dict[str, int | None] | None = None,
    *,
    self_name: str | None = None,
) -> tuple[str, str] | None:
    """从桥表引用的对象里选出两个业务对象端点 ``(source, target)``。

    规则（对齐 convert_object_to_relation 的「两端必须业务对象」约束）：

    1. 候选 = ``ref_targets`` 去重、排除自指(``self_name``)、且角色为
       ``business_object`` 的对象（**保持列出现序**）。非业务对象引用（技术表/其它
       桥表/维度数据表）不作端点——生成期不做 convert 那样的自动提升，避免批量误升。
    2. **<2 个候选** → 返回 ``None``（桥表保持未物化，交由现有一致性告警/人工或
       后续 parent 端解析处理，典型如只引用父表的明细/子表）。
    3. **≥2 个候选** → ``source`` 取**列序第一个**业务对象引用（Frappe 惯例把定义性
       主参与方列在前，如 purchase_invoice.supplier / pos_invoice.customer）；
       ``target`` 取其余候选里「主数据度」最高者——入度(``degree``)降序、平手行数
       (``row_count``)升序、再平手按列序。据此得到「供应商→公司」这类直觉端点，而非
       让无处不在的记账维度（公司/科目）抢占两端。

    纯 in-degree 会系统性选中 company/account 这类每张单据都挂的通用维度而丢掉真正的
    业务主体，故 source 改用列序、仅 target 用主数据度。``ref_targets`` 顺序被视为稳定
    输入（列出现序），保证同一输入产出确定端点。
    """
    degree = degree or {}
    row_count = row_count or {}

    seen: set[str] = set()
    candidates: list[str] = []
    for name in ref_targets:
        if not name or name == self_name or name in seen:
            continue
        seen.add(name)
        if role_of.get(name) == ROLE_BUSINESS_OBJECT:
            candidates.append(name)

    if len(candidates) < 2:
        return None

    source = candidates[0]
    rest = candidates[1:]
    order = {name: i for i, name in enumerate(rest)}

    def master_score(name: str) -> tuple[int, float, int]:
        # 入度越高越像主数据 → 降序（取负）；行数越少越像主数据 → 升序；平手按列序。
        rc = row_count.get(name)
        rc_key = float(rc) if rc is not None else float("inf")
        return (-degree.get(name, 0), rc_key, order[name])

    target = min(rest, key=master_score)
    return source, target
