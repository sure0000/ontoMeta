"""画布智能补录：在**人选中的那几张表之间**直接把线预连出来。

与 ``relation_inference`` 的分工——两者共用同一套机械层（``key_family`` 聚族 +
``key_family_verdict`` 判真伪），交付物完全不同：

- ``relation_inference``：整域跑一遍，产出**键族候选**，人在抽屉里按族表态，
  确认后的族作为关联证据进本体、可写回 DataHub 外键。审的单位是「族」。
- 本模块：只看画布上那几张表，产出**一条条具体的边**（谁→谁、按哪对字段），
  直接摆到画布上。审的单位是「线」——看着图删掉不对的那几根，剩下的上报。

**为什么审的单位必须是线**：一个族在整域里展开动辄几千对，按族审等于让人对
一堆看不见的关系一次性表态；而画布上人本来就在看图，「这根线不对」是一眼的事。

三条取舍：

1. **普遍性闸门参照全域**，不参照选中的表——见
   :func:`key_family.candidate_key_columns` 的说明。选 20 张表不该让 ``ry_bh``
   变成「太普遍」。
2. **一个族在画布上收敛成星形**，不是全连通图：n 张表共用一个键，全连是
   n(n-1)/2 根线，人一样审不动。取区分度最高的成员作参照端，其余各连一根，
   n-1 根线，且这就是这个键真实的语义（大家都指向同一个实体）。
3. **判过的族不再问模型**：``family_id`` 是值形状的哈希，跨作用域稳定，所以
   ``relation_candidate_families`` 里已有的判定在这里直接沿用——人否决过的不再画，
   机器判过的免掉一次 LLM 往返。实测这一步是全程唯一的耗时项（一个族三十秒到一分半），
   而人在画布上是「加两张表再点一次」的用法，每次重问等于每次干等。
   ``refresh=True`` 是重判的出口。

**这个接口会写一处**：新判出来的族按 ``state=proposed`` 落进候选表，作为判定缓存。
写的**只有机器那部分**——人的表态一个字都不碰，也不写 DataHub。落库时用的是
**全域**的成员列表而不是画布这几张表的，否则这行以后被人确认时，
``confirmed_joins`` 只会展开当时画布上那十几张表。
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field, replace

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DomainContext, RelationCandidateFamily
from app.schemas import DatasetInput
from app.services import datahub_bundle_cache, key_family, key_family_verdict
from app.services.key_family import KeyColumn, KeyFamily
from app.services.settings_service import SettingsService

logger = logging.getLogger("ontometa.canvas_suggest")

#: 一次预连线最多画多少根。超过这个数，画布本身就不可读了，人也不会真去审。
MAX_SUGGESTIONS = 200

#: 一次最多分析多少张表。这道闸只挡住荒唐的请求（整域几百上千张一次塞进来），
#: 不挡正常的批量用法——表清单支持一次全选几十上百张放到画布，作用域默认就是
#: 画布上全部，卡在 60 会让人刚批量放完就吃一个报错。真正管可读性的是
#: :data:`MAX_SUGGESTIONS`。
MAX_SCOPE_TABLES = 200

#: 来源标记，前端据此说明这根线是怎么来的。
ORIGIN_KEY_FAMILY = "key_family"
ORIGIN_CONFIRMED = "confirmed_family"

#: 人表过态的族状态（与 ``relation_inference.DECIDED_STATES`` 同一口径）。
_HUMAN_DECIDED = frozenset({"confirmed", "rejected", "applied"})

@dataclass(frozen=True)
class EdgeSuggestion:
    """一根预连出来的线。表名用**调用方传进来的原样**，前端直接拿去对画布节点。"""

    source_table: str
    source_column: str
    target_table: str
    target_column: str
    #: 按 distinct/rows 算出来的，不问模型。
    cardinality: str
    structure_type: str
    confidence: float
    origin: str
    #: 这根线是靠哪个键连的——「人员编号」这种中文名，由 LLM 给。
    key_name: str | None
    entity_name: str | None
    value_shape: str
    #: 判据原文。人点开这根线时看的就是这句。
    reason: str | None


@dataclass
class SuggestionResult:
    suggestions: list[EdgeSuggestion] = field(default_factory=list)
    #: 真正参与分析的表（能在 DataHub 元数据里对上号的）。
    scanned_tables: list[str] = field(default_factory=list)
    #: 对不上号、被跳过的表。**要如实报**——否则人只会看到「没连出线」，不知道为什么。
    skipped_tables: list[str] = field(default_factory=list)
    families: int = 0
    #: 判为 not_a_key / 此前被人否决而没画的族数。
    dismissed_families: int = 0
    cached_bundle: bool = False
    truncated: bool = False


def _bare(name: str) -> str:
    return name.rsplit(".", 1)[-1] if "." in name else name


def _resolve_scope(
    datasets: list[DatasetInput], tables: list[str]
) -> tuple[dict[str, str], list[str]]:
    """请求里的表名 → bundle 里的数据集名。

    返回 ``(bundle 名 → 请求原名, 对不上的请求名)``。画布节点名来自
    ``lineage_inventory``（``库.表``），bundle 名来自 ``fetch_domain_bundle``（可能是裸名），
    两个 DataHub 查询给的形态不一样。按全名优先、裸名次之匹配，且**裸名只在唯一时才认**——
    与 ``lineage_inventory.resolve`` 同一条口径：对不上就丢，不猜。
    """
    by_full: dict[str, str] = {}
    by_bare: dict[str, list[str]] = {}
    for dataset in datasets:
        by_full[dataset.name.lower()] = dataset.name
        by_bare.setdefault(_bare(dataset.name).lower(), []).append(dataset.name)

    resolved: dict[str, str] = {}
    skipped: list[str] = []
    for table in tables:
        key = table.strip().strip("`\"'")
        hit = by_full.get(key.lower())
        if hit is None:
            names = by_bare.get(_bare(key).lower(), [])
            hit = names[0] if len(names) == 1 else None
        if hit is None:
            skipped.append(table)
        else:
            resolved[hit] = table
    return resolved, skipped


def _best_column_per_table(
    family: KeyFamily, scope: set[str]
) -> list[KeyColumn]:
    """族内每张表只留一列：区分度最高的那个。

    同一个族里一张表出现多列（``bl_bh`` 和 ``zbr_bh`` 都是人员编号）时，逐列连线会在
    同一对表之间画出多根语义重复的线。要连复合键，人在画布上再拖一次就是了。
    """
    best: dict[str, KeyColumn] = {}
    for member in family.members:
        if member.table not in scope:
            continue
        current = best.get(member.table)
        if current is None or (member.distinct_ratio, member.distinct) > (
            current.distinct_ratio,
            current.distinct,
        ):
            best[member.table] = member
    return sorted(best.values(), key=lambda m: m.ref)


def _star_edges(
    family: KeyFamily,
    verdict: key_family_verdict.FamilyVerdict,
    scope: set[str],
    origin: str,
) -> list[EdgeSuggestion]:
    """一个族 → 星形的若干条边（参照端为轴）。"""
    picked = _best_column_per_table(family, scope)
    if len(picked) < 2:
        return []

    # 轴 = 最像主数据端的那一列：先看本表内是否近似唯一，再看区分度、基数。
    anchor = max(picked, key=lambda m: (m.near_unique, m.distinct_ratio, m.distinct))
    edges: list[EdgeSuggestion] = []
    for member in picked:
        if member is anchor:
            continue
        # 方向仍交给 orient_reference（明细 → 主数据），不因为选了轴就强行定向：
        # 没有近唯一成员的族方向本来就是靠行数判的，硬摆成星会把方向说死。
        source, target = key_family.orient_reference(member, anchor)
        edges.append(
            EdgeSuggestion(
                source_table=source.table,
                source_column=source.column,
                target_table=target.table,
                target_column=target.column,
                cardinality=key_family.cardinality_between(source, target),
                structure_type=(
                    "bridge_table"
                    if not (source.near_unique or target.near_unique)
                    else "foreign_key"
                ),
                confidence=round(verdict.confidence, 4),
                origin=origin,
                key_name=verdict.key_name or None,
                entity_name=verdict.entity_name or None,
                value_shape=family.value_shape,
                reason=verdict.reason or None,
            )
        )
    return edges


def _stored_verdicts(
    db: Session, domain_id: str
) -> dict[str, RelationCandidateFamily]:
    rows = db.execute(
        select(RelationCandidateFamily).where(
            RelationCandidateFamily.domain_context_id == domain_id
        )
    ).scalars()
    return {row.family_id: row for row in rows}


def _as_verdict(row: RelationCandidateFamily) -> key_family_verdict.FamilyVerdict:
    return key_family_verdict.FamilyVerdict(
        family_id=row.family_id,
        verdict=row.verdict,
        entity_name=row.entity_name or "",
        key_name=row.key_name or "",
        predicate=row.predicate or "",
        confidence=row.confidence or 0.5,
        reason=row.reason or "",
    )


def _remember(
    db: Session,
    domain_id: str,
    run_id: str,
    family: KeyFamily,
    verdict: key_family_verdict.FamilyVerdict,
) -> None:
    """把新判出来的族存成候选行（``state=proposed``），下次不必再问模型。

    只在这个族**还没有行**时写——已有行意味着整域推断或人已经处理过它，那份记录
    才是权威的。``family`` 必须是**全域**那个（成员覆盖全域的表），不是画布子集那个。
    """
    from app.services import relation_inference

    db.add(
        RelationCandidateFamily(
            domain_context_id=domain_id,
            run_id=run_id,
            family_id=family.id,
            value_shape=family.value_shape,
            sample_values_json=json.dumps(
                list(family.sample_values), ensure_ascii=False
            ),
            members_json=relation_inference.members_payload(family),
            table_count=len(family.tables),
            column_count=len(family.members),
            verdict=verdict.verdict,
            entity_name=verdict.entity_name or None,
            key_name=verdict.key_name or None,
            predicate=verdict.predicate or None,
            confidence=verdict.confidence,
            reason=verdict.reason or None,
            state="proposed",
        )
    )


async def suggest(
    db: Session, domain_id: str, tables: list[str], *, refresh: bool = False
) -> SuggestionResult:
    """在给定的这些表之间预连线。

    **不碰 DataHub，也不碰任何人工表态**；唯一的写是把新判出来的族缓存成
    ``state=proposed`` 的候选行（见 :func:`_remember`）。
    """
    domain = db.get(DomainContext, domain_id)
    if domain is None:
        raise ValueError("数据域不存在")
    wanted = [t for t in dict.fromkeys(t.strip() for t in tables) if t]
    if len(wanted) < 2:
        raise ValueError("至少要有两张表才谈得上连线")
    if len(wanted) > MAX_SCOPE_TABLES:
        raise ValueError(
            f"一次最多分析 {MAX_SCOPE_TABLES} 张表（本次 {len(wanted)} 张）；"
            "请先选一小批，或改用整域的关系推断"
        )

    bundle, cached = await datahub_bundle_cache.fetch(db, domain, refresh=refresh)
    resolved, skipped = _resolve_scope(bundle.datasets, wanted)
    result = SuggestionResult(
        scanned_tables=[resolved[name] for name in resolved],
        skipped_tables=skipped,
        cached_bundle=cached,
    )
    if len(resolved) < 2:
        return result

    subset = [ds for ds in bundle.datasets if ds.name in resolved]
    families, _stats = key_family.build_key_families(
        subset, ubiquity_reference=bundle.datasets
    )
    result.families = len(families)
    if not families:
        return result

    stored = _stored_verdicts(db, domain_id)
    reuse: dict[str, key_family_verdict.FamilyVerdict] = {}
    ask: list[KeyFamily] = []
    for family in families:
        row = stored.get(family.id)
        if row is not None and row.state in _HUMAN_DECIDED:
            # 人表过态的族：否决的不再画，确认的直接用。``refresh`` 也不翻人的案——
            # 它重跑的是机器判定，不是人的结论。
            if row.state == "rejected":
                result.dismissed_families += 1
            else:
                reuse[family.id] = _as_verdict(row)
        elif row is None or refresh:
            ask.append(family)
        else:
            # state=proposed：机器判过、人还没看。判定缓存，直接用。
            reuse[family.id] = _as_verdict(row)

    if ask:
        # 落库要的是**全域**的成员，不是画布这几张表的——见 _remember。
        # 这一步纯 CPU（bundle 已在手），不再走网络。
        domain_families = {
            item.id: item for item in key_family.build_key_families(bundle.datasets)[0]
        }
        runtime = SettingsService().get_llm_runtime(db)
        run_id = str(uuid.uuid4())
        for family, verdict in zip(
            ask, await key_family_verdict.judge_families(ask, runtime), strict=True
        ):
            reuse[family.id] = verdict
            if family.id not in stored:
                _remember(
                    db, domain_id, run_id, domain_families.get(family.id) or family, verdict
                )
        db.commit()

    scope = set(resolved)
    for family in families:
        verdict = reuse.get(family.id)
        if verdict is None:
            continue
        if verdict.verdict == key_family_verdict.VERDICT_NOT_A_KEY:
            result.dismissed_families += 1
            continue
        origin = (
            ORIGIN_CONFIRMED
            if (row := stored.get(family.id)) is not None
            and row.state in {"confirmed", "applied"}
            else ORIGIN_KEY_FAMILY
        )
        result.suggestions.extend(_star_edges(family, verdict, scope, origin))

    # 把握高的排前面：截断时先丢最不确定的那些。
    result.suggestions.sort(
        key=lambda s: (-s.confidence, s.source_table, s.target_table)
    )
    if len(result.suggestions) > MAX_SUGGESTIONS:
        result.suggestions = result.suggestions[:MAX_SUGGESTIONS]
        result.truncated = True

    # 表名换回调用方传进来的原样，前端才对得上画布节点。
    result.suggestions = [
        replace(
            item,
            source_table=resolved.get(item.source_table, item.source_table),
            target_table=resolved.get(item.target_table, item.target_table),
        )
        for item in result.suggestions
    ]
    result.scanned_tables = [resolved[name] for name in resolved]

    logger.info(
        "画布预连线 domain=%s 表 %d/%d 族 %d → %d 条线",
        domain.name,
        len(resolved),
        len(wanted),
        result.families,
        len(result.suggestions),
    )
    return result
