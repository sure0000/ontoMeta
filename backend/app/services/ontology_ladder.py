"""阶梯式本体加载（Ladder Loading）：窄而深、迭代收敛。

**为什么存在**：Data Agent 此前的上下文准备有两个极端——
 · 域语义卡把**全域**已发布对象/关系/逻辑读进来算 Top-N（构建成本 O(全域)）；
 · 检索工具每次只回 8 条**骨架**（name/display_name/description），信息太浅——
   要用某个对象取数，还得再调 get_object 拿字段、再调 profile_values 看取值、
   再查物化契约看落在哪张物理表，来回好几步，且每步都可能被截断。

阶梯式加载把「宽而浅」换成「窄而深 + 多轮收敛」：

    第 k 轮：
      1. 用（逐轮放宽的）关键词候选出**最可能相关**的少数本体，带**置信度**；
      2. 只对置信度达标的候选，一次性加载**完整信息包**——
         字段全集 + 关系 + 绑定口径 + 数据样例/统计 + 物化引擎/物理表；
      3. 命中够了就返回；不够则放宽范围进入第 k+1 轮。

    终止条件（任一）：
      · 候选不出任何本体；
      · 最高置信度 < 阈值（宁可不喂，也不喂一堆不相关的把上下文冲淡）；
      · 达到轮数上限 / 已加载实体数达上限（防上下文溢出）。

**每轮范围缩小但信息更全**——这正是与「全量加载」相反的取向：不是一次把域倒进
上下文，而是精确锁定极少数本体、但把这几个讲透。加载出来的完整信息包由调用方
（chat_bi 的 seed 注入或专门的 load_ontology 工具）择要拼进上下文。

数据样例/统计/物化属于**真实数据暴露**，与 run_sql/profile_values 同一道权限闸门：
调用方传 principal_role，权限不足则信息包里这两块降级为不可用说明，不报错。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.models import BusinessLogic, EntityStatus, ObjectType

logger = logging.getLogger("ontometa.ontology_ladder")

_PUB = EntityStatus.PUBLISHED.value

# 阶梯参数（可按域规模调优；置信度是"关键词命中强度"的归一分，0~1）。
_MAX_ROUNDS = 3               # 最多迭代几轮
_MAX_LOADED = 6               # 全程最多深加载几个对象（硬上限，防上下文溢出）
_PER_ROUND_CANDIDATES = 4     # 每轮候选池大小（进入深加载前先排序取前几）
_MIN_CONFIDENCE = 0.15        # 最高候选低于此值即认定"候选不出本体"，终止
_PROFILE_MAX_PROPS = 6        # 每个对象最多为几个字段取数据样例/统计（真实数据，成本高）
_GRAM_STEP = 2                # 每轮新纳入几个实体词 gram 作关键词（逐轮放宽）

# 元问询/虚词停用字：含这些字的中文 2-gram（“有哪/字段/关系/多少”）降权为兜底。
_STOP_CHARS = set(
    "的了有哪些是吗呢什么多少几个和与及在属于包含字段属性关系列表查看想要请帮我告诉"
)


# --------------------------------------------------------------------------- 结果


@dataclass
class LoadedObject:
    """深加载后的单个对象完整信息包。"""

    id: str
    name: str
    display_name: str
    confidence: float
    description: str | None = None
    table_role: str | None = None
    properties: list[dict] = field(default_factory=list)      # 字段全集（含语义类型）
    relations: list[dict] = field(default_factory=list)       # 进出关系
    business_logics: list[dict] = field(default_factory=list)  # 绑定口径
    profiles: list[dict] = field(default_factory=list)        # 数据样例 + 统计（可空）
    materialization: dict | None = None                        # 物化引擎/物理表（可空）

    def to_dict(self) -> dict:
        d = {
            "id": self.id,
            "name": self.name,
            "display_name": self.display_name,
            "confidence": round(self.confidence, 3),
            "description": self.description,
            "table_role": self.table_role,
            "properties": self.properties,
            "relations": self.relations,
            "business_logics": self.business_logics,
        }
        if self.profiles:
            d["value_profiles"] = self.profiles
        if self.materialization is not None:
            d["materialization"] = self.materialization
        return d


@dataclass
class LadderResult:
    """一次阶梯式加载的结果与可观测轨迹。"""

    objects: list[LoadedObject] = field(default_factory=list)
    logics: list[dict] = field(default_factory=list)
    rounds_used: int = 0
    stop_reason: str = ""              # matched / low_confidence / no_candidate / budget
    round_trace: list[dict] = field(default_factory=list)  # 每轮的候选与置信度，供审计

    def to_dict(self) -> dict:
        return {
            "objects": [o.to_dict() for o in self.objects],
            "logics": self.logics,
            "rounds_used": self.rounds_used,
            "stop_reason": self.stop_reason,
            "round_trace": self.round_trace,
            "note": (
                "阶梯式加载：每轮锁定最相关的少数本体并深加载其完整信息"
                "（字段/关系/口径/取值样例/物化）。objects 已是当前问题最相关的实体，"
                "可直接据此作答或取数；如需域内其它实体，再用 search_* 检索。"
            ),
        }


# --------------------------------------------------------------------------- 分词与置信度


def _tokens(text: str) -> list[str]:
    """中英混合分词：英文按词，中文按 2-gram。

    中文用 2-gram 而非单字：“订单”该作为一个 token 参与匹配，而不是拆成
    “订”“单”——单字 ILIKE 命中太宽且偿然（“单”能命中“订单/单据/名单”）。
    与 ``domain_semantic_card`` / ``agents/common.py`` 同口径。
    """
    import re

    if not text:
        return []
    lowered = text.lower()
    latin = re.findall(r"[a-z0-9_]+", lowered)
    han_runs = re.findall(r"[\u4e00-\u9fa5]+", lowered)
    grams: list[str] = []
    for run in han_runs:
        if len(run) == 1:
            grams.append(run)
        else:
            grams.extend(run[i : i + 2] for i in range(len(run) - 1))
    return latin + grams


def _confidence(query_tokens: set[str], *fields: str | None) -> float:
    """候选名称/描述与问题的重合度，归一到 0~1。

    归一相对**候选名**的 token（而非整个问题）：候选的词被问题覆盖得越多，
    越可能是它——否则长问题会把分母撇大、把真正命中的实体分压得很低。
    只看名称（display_name/name）；描述只在名称零命中时作弱信号补。
    """
    if not query_tokens:
        return 0.0
    name_fields = [f for f in fields[:2] if f]
    name_tokens = set(_tokens(" ".join(name_fields)))
    if name_tokens:
        overlap = len(query_tokens & name_tokens)
        if overlap:
            return min(overlap / len(name_tokens), 1.0)
    # 名称零命中：退而看描述（弱信号，封顶 0.4）。
    desc = fields[2] if len(fields) > 2 else None
    if desc:
        desc_tokens = set(_tokens(desc))
        overlap = len(query_tokens & desc_tokens)
        if overlap:
            return min(0.4 * overlap / max(len(query_tokens), 1), 0.4)
    return 0.0


# --------------------------------------------------------------------------- 加载器


class OntologyLadderLoader:
    """阶梯式加载的编排者。检索/详情/画像/物化的具体取数委托给既有服务，本类只管**收敛策略**。"""

    def __init__(self, query_service: Any | None = None) -> None:
        if query_service is None:
            # 与 chat_bi 同源的聚合门面（同时拥有 list_object_types / list_business_logics）。
            from app.services.query import OntologyQueryService

            query_service = OntologyQueryService()
        self.qs = query_service

    # ---- 主入口 -------------------------------------------------------------

    def load(
        self,
        db: Session,
        *,
        domain_id: str,
        ontology_id: str,
        question: str,
        principal_role: str | None = None,
        want: int = 2,
        with_profiles: bool = True,
    ) -> LadderResult:
        """对 ``question`` 做阶梯式加载，返回最相关的至多 ``want`` 个对象的完整信息包。

        ``with_profiles=False`` 时跳过数据样例/统计（纯元数据场景，省真实数据查询）。
        """
        q_tokens = set(_tokens(question))
        entity_grams = self._entity_grams(question)
        result = LadderResult()
        loaded_ids: set[str] = set()

        for round_idx in range(_MAX_ROUNDS):
            result.rounds_used = round_idx + 1
            # 逐轮放宽：第 k 轮纳入前 (k+1)×GRAM_STEP 个实体词作关键词，
            # 每个词单独查库后合并（ILIKE 是子串匹配，不能拼串当 OR）。
            gram_budget = (round_idx + 1) * _GRAM_STEP
            keywords = entity_grams[:gram_budget]
            candidates = self._candidates(
                db, domain_id=domain_id, keywords=keywords, question=question
            )
            result.round_trace.append({
                "round": round_idx + 1,
                "keywords": keywords,
                "candidates": [
                    {"display_name": c[1].display_name, "confidence": round(c[0], 3)}
                    for c in candidates
                ],
            })

            if not candidates:
                result.stop_reason = "no_candidate"
                if gram_budget >= len(entity_grams):
                    break  # 词已用尽，再放宽也无新词
                continue

            top_conf = candidates[0][0]
            if top_conf < _MIN_CONFIDENCE:
                # 最相关的都不够像 → 本轮不深加载，放宽再试；始终不达标则以此为终因。
                result.stop_reason = "low_confidence"
                if gram_budget >= len(entity_grams):
                    break
                continue

            # 深加载：对达标候选逐个组装完整信息包，直到满足 want 或触及硬上限。
            for conf, obj in candidates:
                if conf < _MIN_CONFIDENCE:
                    break
                if obj.id in loaded_ids:
                    continue
                if len(result.objects) >= min(want, _MAX_LOADED):
                    break
                pkg = self._deep_load(
                    db,
                    obj=obj,
                    confidence=conf,
                    ontology_id=ontology_id,
                    principal_role=principal_role,
                    with_profiles=with_profiles,
                )
                result.objects.append(pkg)
                loaded_ids.add(obj.id)

            if len(result.objects) >= min(want, _MAX_LOADED):
                result.stop_reason = "matched"
                break

        if result.objects:
            # 只要加载到对象就算命中（即使未填满 want），不该拿末轮的 no_candidate 当结果。
            if result.stop_reason not in ("matched",):
                result.stop_reason = "budget"
        elif not result.stop_reason:
            result.stop_reason = "no_candidate"

        # 顺带带回强相关口径（同一套实体词，浅层即可——口径要展开另有 compile_metric）。
        result.logics = self._match_logics(db, domain_id=domain_id, keywords=entity_grams)
        return result

    # ---- 实体词提取（过滤元问询噪声） -----------------------------------

    @staticmethod
    def _entity_grams(question: str) -> list[str]:
        """从问题里提出**实体候选词**，按“越可能是实体”排序。

        英文词优先（信息量大）→ 不含停用字的中文 2-gram（如“订单”）→
        含停用字的 gram（“有哪/字段/关系”等元问询噪声，作兜底）。
        这样召回优先锚在实体名上，而不是被“字段/关系”这类词带偏。
        """
        toks = _tokens(question)
        latin = [t for t in toks if t.isascii()]
        han_grams = [t for t in toks if not t.isascii()]
        clean = [g for g in han_grams if not (set(g) & _STOP_CHARS)]
        noisy = [g for g in han_grams if set(g) & _STOP_CHARS]
        seen: set[str] = set()
        ordered: list[str] = []
        for g in [*latin, *clean, *noisy]:
            if g and g not in seen:
                seen.add(g)
                ordered.append(g)
        return ordered

    # ---- 候选与置信度 -------------------------------------------------------

    def _candidates(
        self, db: Session, *, domain_id: str, keywords: list[str], question: str
    ) -> list[tuple[float, ObjectType]]:
        """逐个关键词独立查库（ILIKE，不加载全量）并合并，再按置信度排序。"""
        merged: dict[str, Any] = {}
        for kw in keywords:
            if not kw:
                continue
            page = self.qs.list_object_types(
                db,
                domain_context_id=domain_id,
                published_only=True,
                q=kw,
                limit=_PER_ROUND_CANDIDATES * 2,
            )
            for summary in page.items:
                oid = getattr(summary, "id", None)
                if oid and oid not in merged:
                    merged[oid] = summary
        if not merged:
            return []

        q_lower = (question or "").lower()
        scored: list[tuple[float, str]] = []
        for oid, summary in merged.items():
            conf = _confidence(
                set(_tokens(question)),
                getattr(summary, "display_name", None),
                getattr(summary, "name", None),
                getattr(summary, "description", None),
            )
            # 整名子串出现在问题里 → 置信度提到高档（问题明确点名了它）。
            for nm in (getattr(summary, "display_name", ""), getattr(summary, "name", "")):
                nm = (nm or "").lower()
                if nm and len(nm) >= 2 and nm in q_lower:
                    conf = max(conf, 0.9)
            scored.append((conf, oid))
        scored.sort(key=lambda x: x[0], reverse=True)
        top = scored[:_PER_ROUND_CANDIDATES]

        # 一次性把选中的 ORM 对象取出（避免逐个 get）。
        ids = [oid for _, oid in top]
        if not ids:
            return []
        objs = {
            o.id: o
            for o in db.query(ObjectType).filter(ObjectType.id.in_(ids)).all()
        }
        return [(conf, objs[oid]) for conf, oid in top if oid in objs]

    # ---- 深加载：完整信息包 -------------------------------------------------

    def _deep_load(
        self,
        db: Session,
        *,
        obj: ObjectType,
        confidence: float,
        ontology_id: str,
        principal_role: str | None,
        with_profiles: bool,
    ) -> LoadedObject:
        """组装单个对象的完整信息：字段 + 关系 + 口径 + 数据画像 + 物化。"""
        detail = self.qs.get_object_type(db, obj.id)

        properties = [
            {
                "id": p.id,
                "name": p.name,
                "display_name": p.display_name,
                "data_type": p.data_type,
                "semantic_type": p.semantic_type,
                "required": p.required,
                "description": p.description,
            }
            for p in (getattr(detail, "properties", None) or [])
        ]
        relations = [
            {
                "name": r.name,
                "display_name": r.display_name,
                "source_object_name": getattr(r, "source_object_name", None),
                "target_object_name": getattr(r, "target_object_name", None),
                "cardinality": getattr(r, "cardinality", None),
            }
            for r in (
                (getattr(detail, "outgoing_relations", None) or [])
                + (getattr(detail, "incoming_relations", None) or [])
            )
        ]
        logics = [
            {
                "id": l.get("id") if isinstance(l, dict) else getattr(l, "id", None),
                "display_name": l.get("display_name")
                if isinstance(l, dict)
                else getattr(l, "display_name", None),
                "expression_summary": l.get("expression_summary")
                if isinstance(l, dict)
                else getattr(l, "expression_summary", None),
            }
            for l in (getattr(detail, "business_logics", None) or [])
        ]

        pkg = LoadedObject(
            id=obj.id,
            name=obj.name,
            display_name=obj.display_name,
            confidence=confidence,
            description=obj.description,
            table_role=obj.table_role,
            properties=properties,
            relations=relations,
            business_logics=logics,
        )

        # 物化引擎/物理表：纯元数据，无权限门槛。
        pkg.materialization = self._load_materialization(db, ontology_id, obj.id)

        # 数据样例 + 统计：真实数据暴露，走 run_sql 同一道权限闸门。
        if with_profiles:
            pkg.profiles = self._load_profiles(
                db,
                ontology_id=ontology_id,
                obj=obj,
                properties=properties,
                principal_role=principal_role,
            )
        return pkg

    def _load_materialization(
        self, db: Session, ontology_id: str, object_id: str
    ) -> dict | None:
        """物化契约：落在哪个引擎、哪张物理表、更新策略。缺失即返回 None。"""
        try:
            from app.services.materialization_contract import (
                MaterializationContractService,
            )

            svc = MaterializationContractService()
            contract = svc.get_for_target(
                db, ontology_id, "object_type", object_id
            )
            if contract is None:
                return None
            return {
                "materialized": bool(contract.materialized),
                "engines": contract.engines,
                "layer": contract.target_layer,
                "load_strategy": contract.load_strategy,
                "partition_key": contract.partition_key,
                "refresh_cron": contract.refresh_cron,
            }
        except Exception as exc:  # noqa: BLE001 — 物化信息是增强，取不到不影响主体
            logger.info("ladder materialization skipped for %s: %s", object_id, exc)
            return None

    def _load_profiles(
        self,
        db: Session,
        *,
        ontology_id: str,
        obj: ObjectType,
        properties: list[dict],
        principal_role: str | None,
    ) -> list[dict]:
        """对对象的关键字段取数据样例 + 统计。

        **优先级**：
          1. 本地 Property.sample_values_json / unique_count（DataHub 导入时沉淀）
          2. column_profiler SQL 现算（无本地数据或需最新值时兜底）

        权限区分：本地 profiling 是导入时已沉淀的**静态元数据**（与字段名/描述同级），
        无需 run_sql 权限；只有 **SQL 现算兜底**（实时读源库）才受 run_sql 同道门槛约束。
        """
        # 只画像"值得画像"的字段（技术字段/未知类型跳过），且封顶 _PROFILE_MAX_PROPS 个。
        from app.services.column_profiler import strategy_for

        picked = [
            p
            for p in properties
            if strategy_for_safe(strategy_for, p.get("semantic_type")) != "skipped"
        ][:_PROFILE_MAX_PROPS]

        # 一次性取出这些字段的 ORM Property 对象，读本地 profiling（不受权限门槛）。
        from app.models import Property

        prop_ids = [p["id"] for p in picked if p.get("id")]
        props_orm = (
            db.query(Property).filter(Property.id.in_(prop_ids)).all()
            if prop_ids
            else []
        )
        prop_by_id = {p.id: p for p in props_orm}

        out: list[dict] = []
        missing_profiling: list[dict] = []  # 本地无 profiling 的字段，留给 SQL 现算兜底

        for p in picked:
            prop_orm = prop_by_id.get(p.get("id"))
            if prop_orm and (prop_orm.sample_values_json or prop_orm.unique_count is not None):
                # 本地有 profiling，直接返回（避免源库查询）。
                sample_values = _loads(prop_orm.sample_values_json) or []
                out.append(
                    {
                        "property_id": p["id"],
                        "property_name": p["name"],
                        "property_display_name": p.get("display_name"),
                        "available": True,
                        "source": "datahub_profiling",
                        "distinct_count": prop_orm.unique_count,
                        "top_values": [
                            {"value": v, "count": None} for v in sample_values
                        ],
                        "note": "来自 DataHub 导入时沉淀的 profiling 元数据。",
                    }
                )
            else:
                # 本地无 profiling，标记为需 SQL 现算兜底。
                missing_profiling.append(p)

        # 兜底：对本地无 profiling 的字段 SQL 现算（成本高，但能拿到最新值）。
        # 现算=实时读源库，受 run_sql 同道权限门槛约束（与本地静态 profiling 不同）。
        if missing_profiling:
            from app.config import settings as env_settings
            from app.models.principal import role_satisfies

            min_role = getattr(env_settings, "agent_run_sql_min_role", None)
            may_query = (not min_role) or role_satisfies(principal_role, min_role)
            if not may_query:
                # 无权现算：本地有的已返回；若全无，给一条降级说明。
                if not out:
                    return [
                        {
                            "available": False,
                            "note": (
                                f"字段无本地 profiling，且当前角色无权实时读取真实取值"
                                f"（需 {min_role} 及以上）；请勿据此臆测取值。"
                            ),
                        }
                    ]
                return out
            try:
                from app.services import data_app_executor
                from app.services.column_profiler import profile_property
                from app.services.ontology_projection import build_projection
            except Exception as exc:  # noqa: BLE001
                logger.info("ladder profiling deps unavailable: %s", exc)
                return out  # 返回已有的本地 profiling

            source = self._resolve_domain_data_source(db)
            if source is None:
                # 无数据源，本地有的已返回，缺失的加一条降级说明。
                if not out:
                    return [
                        {
                            "available": False,
                            "note": "当前数据域无可执行数据源，且本地无 DataHub profiling，无法读取真实取值样例。",
                        }
                    ]
                return out  # 有部分本地 profiling 就返回，不在意缺失部分

            proj = build_projection(db, ontology_id, None)
            objv = proj.object_of(obj.name)
            if objv is None:
                return out

            mapping = _loads(source.mapping_json)
            dsn = source.dsn_secret_ref
            backend = data_app_executor.backend_of(dsn)

            for p in missing_profiling:
                propv = objv.resolve_property(p["name"])
                if propv is None:
                    continue
                try:
                    profile = profile_property(
                        proj,
                        objv,
                        propv,
                        dsn=dsn,
                        mapping=mapping,
                        backend=backend,
                        scope_key=f"{ontology_id}|{getattr(source, 'id', '')}",
                    )
                    out.append(profile.to_dict())
                except Exception as exc:  # noqa: BLE001 — 单字段画像失败不影响其它
                    logger.info(
                        "ladder profile failed for %s.%s: %s", obj.name, p["name"], exc
                    )

        return out

    # ---- 口径浅匹配 ---------------------------------------------------------

    def _match_logics(
        self, db: Session, *, domain_id: str, keywords: list[str]
    ) -> list[dict]:
        """强相关口径（浅层）：逐个实体词 ILIKE 查库合并取前几条，展开留给 compile_metric。"""
        if not keywords:
            return []
        merged: dict[str, Any] = {}
        for kw in keywords[:_GRAM_STEP]:  # 只用最像实体的前几个词，避免元问询词拉进无关口径
            page = self.qs.list_business_logics(
                db, domain_context_id=domain_id, published_only=True, q=kw, limit=3
            )
            for l in page.items:
                lid = getattr(l, "id", None)
                if lid and lid not in merged:
                    merged[lid] = l
        return [
            {
                "id": getattr(l, "id", None),
                "name": getattr(l, "name", None),
                "display_name": getattr(l, "display_name", None),
                "logic_type": getattr(l, "logic_type", None),
                "expression_summary": getattr(l, "expression_summary", None),
            }
            for l in list(merged.values())[:3]
        ]

    # ---- 数据源解析（统一走 services.data_app 的 warehouse-first 策略） ----------

    @staticmethod
    def _resolve_domain_data_source(db: Session):
        from app.services.data_app import resolve_domain_data_source

        return resolve_domain_data_source(db)


# --------------------------------------------------------------------------- 小工具


def _loads(raw: str | None) -> dict | None:
    if not raw:
        return None
    import json

    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def strategy_for_safe(strategy_for, semantic_type: Any) -> str:
    """把字符串语义类型喂给 column_profiler.strategy_for；异常一律当 skipped。"""
    try:
        from app.ontology_types import SemanticType

        if isinstance(semantic_type, str):
            semantic_type = SemanticType(semantic_type)
        return strategy_for(semantic_type)
    except Exception:  # noqa: BLE001
        return "skipped"


__all__ = ["OntologyLadderLoader", "LadderResult", "LoadedObject"]
