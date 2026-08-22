"""本体三方合并（base/ours/theirs）：让再生成不破坏人工修正。

设计见 ONTOLOGY_VERSIONING_PLAN.md。核心思想：
- base   = 上一次机器生成的基线值（entity.machine_baseline）
- ours   = 当前库中的值（可能含人工修正）
- theirs = 本次机器新生成的值

逐字段规则：
- 人未改（ours==base 且未钉住） → 采纳机器新值 theirs
- 人改过、机器没变（theirs==base） → 保留人工值
- 双方都改（冲突） → 保留人工值，记入 conflict，交人工复核
- 基线始终推进到 theirs（冲突只提示一次）

人工新建（user_created）不被机器覆盖/删除；人工删除（deleted_by_user）不被复活；
上游消失的纯机器实体删除，含人工价值的置 deprecated 并标记 upstream_removed。
"""

from __future__ import annotations

import json
from typing import Any, Iterable

from sqlalchemy.orm import Session

from app.models import (
    DraftEvidence,
    EntityStatus,
    ObjectType,
    Property,
    RelationType,
)
from app.schemas import (
    DraftObjectType,
    DraftProperty,
    DraftRelationType,
    OntologyDraftOutput,
)
from app.services.object_naming import dedupe_object_names, table_name_from_ref

# 各实体的可合并字段（与 alembic c1d2e3f4a5b6 回填保持一致）
OBJECT_FIELDS = ["name", "display_name", "description", "table_role", "role_reason"]
PROPERTY_FIELDS = ["display_name", "description", "data_type", "semantic_type"]
RELATION_FIELDS = ["display_name", "description", "cardinality", "structure_type"]
LOGIC_FIELDS = ["display_name", "description", "expression_summary", "logic_type"]

# 描述性字段：纯说明文字，下游不依赖，机器可持续刷新（除非人工显式改过而被钉住）。
# 其余可合并字段都是**结构性**的——投影、建表、SQL 生成都读它们，改动即破坏性变更，
# 故实体一经发布，其结构性字段即升为人工权威（见 publish.py 的 seed_published_authority）。
DESCRIPTIVE_FIELDS = frozenset({"description", "role_reason"})


def structural_fields(fields: Iterable[str]) -> list[str]:
    """从可合并字段里筛出结构性字段。"""
    return [f for f in fields if f not in DESCRIPTIVE_FIELDS]


def relation_signature(
    source_ref: str | None, target_ref: str | None, structure_type: str | None
) -> str | None:
    """关系稳定身份键：urn(src)|urn(tgt)|structure_type。两端 urn 缺失时返回 None
    （调用方回退到 name 匹配）。"""
    if source_ref and target_ref:
        return f"{source_ref}|{target_ref}|{structure_type or ''}"
    return None


def _loads(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return default


def _dumps(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (list, dict)) and not value:
        return None
    return json.dumps(value, ensure_ascii=False)


_MERGEABLE_FIELDS_BY_ENTITY = {
    "ObjectType": OBJECT_FIELDS,
    "Property": PROPERTY_FIELDS,
    "RelationType": RELATION_FIELDS,
    "BusinessLogic": LOGIC_FIELDS,
}


def seed_published_authority(entity: Any) -> list[str]:
    """把实体的结构性字段钉住——「已发布即人工权威」的落地，发布时调用。

    这是三方合并唯一听得懂的语言：钉住的字段机器不得直接改，只会记冲突交人工裁决。
    一域一本体后再生成会直接打在正对外服务的那一行上，没有这一步，人没手动改过的
    已发布字段会被机器静默覆盖——生产内容在无人点发布的情况下变了。

    刻意**不推进** ``machine_baseline``：基线要停在机器上次的输出上，机器再次给出同一个
    值时 ``inc == base`` 即静默保留人工值；若把基线推到已发布值，机器每跑一次都会重报
    同一条冲突，"冲突只提示一次"的约定就破了。

    返回本次新钉住的字段名。``origin`` 不动——钉住表达的是权威，不是作者身份。
    """
    fields = _MERGEABLE_FIELDS_BY_ENTITY.get(type(entity).__name__)
    if not fields or not hasattr(entity, "overridden_fields"):
        return []
    pinned = set(_loads(entity.overridden_fields, []))
    newly = [f for f in structural_fields(fields) if f not in pinned]
    if not newly:
        return []
    pinned.update(newly)
    entity.overridden_fields = _dumps(sorted(pinned))
    return newly


def _prop_key(name: str | None) -> str:
    """属性去重键：字段名在对象内唯一，故按归一化字段名去重（不用会漂移的 source_field_ref）。"""
    return (name or "").strip().lower()


def _new_report_section() -> dict[str, list]:
    return {"added": [], "updated": [], "kept": [], "conflict": [], "removed": []}


class MergeReport:
    """累积各实体段的合并结果，供任务层落库与前端展示。"""

    def __init__(self) -> None:
        self.sections = {
            "object_types": _new_report_section(),
            "properties": _new_report_section(),
            "relation_types": _new_report_section(),
            "business_logics": _new_report_section(),
        }

    def record(
        self,
        section: str,
        outcome: str,
        entity_id: str,
        name: str,
        display_name: str,
        *,
        fields: list[str] | None = None,
        conflicts: dict | None = None,
    ) -> None:
        if outcome == "skip_user":
            return
        item = {"id": entity_id, "name": name, "display_name": display_name}
        if fields:
            item["fields"] = fields
        if conflicts:
            item["conflicts"] = conflicts
        self.sections[section][outcome].append(item)

    def to_dict(self) -> dict:
        summary = {"added": 0, "updated": 0, "kept": 0, "conflict": 0, "removed": 0}
        for section in self.sections.values():
            for key in summary:
                summary[key] += len(section.get(key, []))
        return {**self.sections, "summary": summary}


def _merge_entity_fields(
    entity: Any, incoming: dict[str, Any], fields: Iterable[str]
) -> tuple[str, list[str], dict]:
    """对单个实体逐字段执行三方合并，就地修改 entity 并推进基线。

    返回 (outcome, machine_changed_fields, conflicts)：
    outcome ∈ {updated, kept, conflict}。
    """
    baseline = _loads(entity.machine_baseline, {})
    overridden = set(_loads(entity.overridden_fields, []))
    conflicts_existing = _loads(entity.conflict_json, {})

    machine_changed: list[str] = []
    new_conflicts: dict = {}

    for f in fields:
        cur = getattr(entity, f)
        base = baseline.get(f)
        inc = incoming.get(f)
        pinned = f in overridden
        user_changed = pinned or (cur != base)

        if user_changed:
            if inc != base and inc != cur:
                new_conflicts[f] = {"base": base, "ours": cur, "theirs": inc}
            # 机器未变或与人工值一致：保留人工值
        else:
            if inc != cur:
                setattr(entity, f, inc)
                machine_changed.append(f)
        baseline[f] = inc  # 基线始终推进

    entity.machine_baseline = _dumps(baseline)

    if new_conflicts:
        conflicts_existing.update(new_conflicts)
    # 已被人工解决/机器回归的字段：如该字段不再冲突则清理
    conflicts_existing = {
        f: v for f, v in conflicts_existing.items() if f in new_conflicts or f in overridden
    }
    entity.conflict_json = _dumps(conflicts_existing)

    if new_conflicts:
        outcome = "conflict"
    elif machine_changed:
        outcome = "updated"
    else:
        outcome = "kept"
    return outcome, machine_changed, new_conflicts


def resolve_duplicate_object_names(
    db: Session, ontology_id: str
) -> list[tuple[str, str, str]]:
    """把一个本体内撞名的对象消歧，就地改 ``name``（调用方负责 commit/flush）。

    生成端（A）只在单个证据块内去碰撞；分块生成时同名的两张表可能落在不同块，
    合并后仍会撞名。故合并末尾与存量脚本都调用此全本体 sweep 兜底。

    规则：撞名组交给 :func:`dedupe_object_names`（撞名成员改用源表名 snake）；但
    **用户改过 name 的对象不动**（尊重人工命名），让其非钉住的同名兄弟去改名。
    改名同时推进 machine_baseline["name"]，避免下次重跑把它当机器改动/冲突。

    返回 ``[(object_id, old_name, new_name), ...]``。
    """
    objs = db.query(ObjectType).filter(ObjectType.ontology_id == ontology_id).all()
    mapping = dedupe_object_names([(o.id, o.name, o.source_ref) for o in objs])
    changes: list[tuple[str, str, str]] = []
    pinned = {o.id for o in objs if "name" in set(_loads(o.overridden_fields, []))}

    def _rename(obj: ObjectType, new_name: str) -> None:
        old_name = obj.name
        obj.name = new_name
        baseline = _loads(obj.machine_baseline, {})
        baseline["name"] = new_name
        obj.machine_baseline = _dumps(baseline)
        changes.append((obj.id, old_name, new_name))

    for obj in objs:
        new_name = mapping.get(obj.id, obj.name)
        if new_name == obj.name or obj.id in pinned:
            continue  # 人工命名，保留
        _rename(obj, new_name)

    # 残留撞名：一组里**全是**人工命名时上面无人退让，重名会一路活到库层唯一约束
    # （uq_object_type_ontology_name）炸成 IntegrityError，整次合并回滚。而重名本身
    # 比丢一个手写名字更糟——投影按名字建索引，重名等于 SQL 打到哪张表全看入库顺序。
    # 故最后强制让出：组内保留第一个，其余按源表名 + 数字后缀消歧。
    by_name: dict[str, list[ObjectType]] = {}
    for obj in objs:
        by_name.setdefault(obj.name, []).append(obj)
    used = set(by_name)
    for name, members in by_name.items():
        if len(members) < 2:
            continue
        for obj in members[1:]:
            base = table_name_from_ref(obj.source_ref) or name
            final, suffix = base, 2
            while final in used:
                final = f"{base}_{suffix}"
                suffix += 1
            used.add(final)
            _rename(obj, final)
    return changes


class OntologyMergeService:
    """把机器生成结果（theirs）合并进已有草稿本体，保护人工修正。"""

    # ------------------------------------------------------------------
    # 对象 + 属性
    # ------------------------------------------------------------------
    def merge_objects(
        self,
        db: Session,
        ontology_id: str,
        object_types: list[DraftObjectType],
        properties: list[DraftProperty],
        gen_id: str | None,
        report: MergeReport,
        *,
        handle_removal: bool = False,
    ) -> dict[str, str]:
        """按 source_ref 合并对象、按 (对象, source_field_ref/name) 合并属性。

        返回 source_ref -> object_type_id，供关系按 urn 回链。
        """
        existing_objs = (
            db.query(ObjectType).filter(ObjectType.ontology_id == ontology_id).all()
        )
        existing_by_ref = {o.source_ref: o for o in existing_objs if o.source_ref}

        # 库层唯一约束 uq_object_type_ontology_name 在 **flush 时**就拦，等不到本方法
        # 末尾的兜底 sweep。故新建/改名前就地占名——规则与
        # :func:`resolve_duplicate_object_names` 一致（撞名改用源表名 snake，仍撞则
        # 追加数字后缀），末尾 sweep 因此退化为对存量重名的兜底。
        used_names = {o.name for o in existing_objs}

        def _allocate(desired: str, source_ref: str | None) -> str:
            """在本体内取一个未被占用的标识名。调用方需先释放该对象的旧名。"""
            if desired not in used_names:
                used_names.add(desired)
                return desired
            base = table_name_from_ref(source_ref) or desired
            final, suffix = base, 2
            while final in used_names:
                final = f"{base}_{suffix}"
                suffix += 1
            used_names.add(final)
            return final

        object_ref_to_id: dict[str, str] = {}
        object_id_by_name: dict[str, str] = {}
        seen_object_ids: set[str] = set()
        # 改名必须分两阶段落库。若 A 释放名称 x、B 同批接管 x，仅在内存里把
        # A.name/B.name 直接改成最终值并不能保证安全：SQLAlchemy 可能按主键先 UPDATE B，
        # SQLite 又逐条检查唯一约束，于是会在 A 尚未释放 x 的中间态报 IntegrityError。
        # 第一阶段统一写入事务内唯一的临时名并 flush，第二阶段才写最终名。
        pending_renames: list[tuple[ObjectType, str]] = []

        def _stage_rename(obj: ObjectType, previous_name: str, final_name: str) -> None:
            if final_name == previous_name:
                obj.name = final_name
                return
            temp_name = f"__ontometa_tmp__{obj.id}"
            suffix = 2
            while temp_name in used_names:
                temp_name = f"__ontometa_tmp__{obj.id}_{suffix}"
                suffix += 1
            used_names.add(temp_name)
            obj.name = temp_name
            pending_renames.append((obj, final_name))

        for item in object_types:
            existing = existing_by_ref.get(item.source_ref) if item.source_ref else None
            incoming = {
                "name": item.name,
                "display_name": item.display_name,
                "description": item.description,
                "table_role": item.table_role,
                "role_reason": item.role_reason,
            }
            if existing is None:
                # 占名后回写 incoming，让 machine_baseline 记的是**实际落库的名字**：
                # 否则下次重跑会把「机器还叫原名」当成机器改动，又改回撞名的那个。
                incoming["name"] = _allocate(item.name, item.source_ref)
                obj = ObjectType(
                    ontology_id=ontology_id,
                    name=incoming["name"],
                    display_name=item.display_name,
                    description=item.description,
                    source_ref=item.source_ref,
                    source_confidence=item.confidence,
                    row_count=item.row_count,
                    table_role=item.table_role,
                    role_confidence=item.role_confidence,
                    role_reason=item.role_reason,
                    # 复核状态只在**新建**时取机器建议。再生成绝不回写——人确认过的
                    # 对象不能被机器重新打成待复核（那会让它悄悄掉出下次发布集）。
                    needs_review=item.needs_review,
                    role_signals=(
                        _dumps(item.role_signals)
                        if item.role_signals is not None
                        else None
                    ),
                    status=EntityStatus.SUGGESTED.value,
                    origin="machine",
                    machine_baseline=_dumps(incoming),
                    last_generation_id=gen_id,
                )
                # INSERT 也可能复用本批已有对象刚释放的名字；先单独刷出临时改名，
                # 避免 UOW 选择 INSERT-before-UPDATE 时撞上数据库中的旧名。
                if pending_renames:
                    db.flush()
                db.add(obj)
                db.flush()
                report.record(
                    "object_types", "added", obj.id, obj.name, obj.display_name
                )
            else:
                obj = existing
                if obj.user_created:
                    obj.last_generation_id = gen_id
                    report.record(
                        "object_types", "skip_user", obj.id, obj.name, obj.display_name
                    )
                else:
                    previous_name = obj.name
                    outcome, changed, conflicts = _merge_entity_fields(
                        obj, incoming, OBJECT_FIELDS
                    )
                    # 三方合并可能把机器名回填到 obj.name，撞上别的对象照样 flush 报错。
                    final_name = obj.name
                    if final_name != previous_name:
                        used_names.discard(previous_name)
                        final_name = _allocate(final_name, obj.source_ref)
                        _stage_rename(obj, previous_name, final_name)
                        baseline = _loads(obj.machine_baseline, {})
                        baseline["name"] = final_name
                        obj.machine_baseline = _dumps(baseline)
                    obj.source_confidence = item.confidence
                    obj.role_confidence = item.role_confidence
                    # 机器证据每次重算直接覆盖（非用户可编辑、不进冲突流程）；
                    # 防御性 None 不覆盖既有值。
                    if item.role_signals is not None:
                        obj.role_signals = _dumps(item.role_signals)
                    obj.last_generation_id = gen_id
                    obj.upstream_removed = False
                    obj.origin = (
                        "machine_edited"
                        if _loads(obj.overridden_fields, [])
                        else "machine"
                    )
                    report.record(
                        "object_types", outcome, obj.id, final_name, obj.display_name,
                        fields=changed, conflicts=conflicts,
                    )
            if item.source_ref:
                object_ref_to_id[item.source_ref] = obj.id
            object_id_by_name[item.name] = obj.id
            seen_object_ids.add(obj.id)

        if pending_renames:
            # 阶段一：所有旧名都已真正从唯一索引中释放。
            db.flush()
            for obj, final_name in pending_renames:
                used_names.discard(obj.name)  # 释放仅供事务内部使用的临时名
                obj.name = final_name
            # 阶段二：最终名已经过 _allocate 全局分配，彼此唯一，可安全批量写入。
            db.flush()

        self._merge_properties(db, object_id_by_name, properties, gen_id, report)

        if handle_removal:
            self._handle_object_removal(db, existing_objs, seen_object_ids, gen_id, report)

        # 兜底去碰撞：分块生成时同名的两张不同表可能分属不同块，合并后才撞名。
        # 先 flush 让新建对象可见（会话可能关闭 autoflush），再全本体消歧。
        db.flush()
        for obj_id, old_name, new_name in resolve_duplicate_object_names(db, ontology_id):
            report.record(
                "object_types", "updated", obj_id, new_name, new_name, fields=["name"]
            )

        return object_ref_to_id

    def _merge_properties(
        self,
        db: Session,
        object_id_by_name: dict[str, str],
        properties: list[DraftProperty],
        gen_id: str | None,
        report: MergeReport,
    ) -> None:
        object_ids = list(object_id_by_name.values())
        # 按字段名去重（字段名在对象内唯一）。历史上用 `source_field_ref or name` 作键，
        # 但 source_field_ref 会随源 URN/命名在多次重跑间漂移，导致同名字段被判成不同项、
        # 每重跑一次就追加一份（creation×3 等累积）。字段名才是对象内稳定唯一标识。
        existing_props: dict[str, dict[str, Property]] = {}
        if object_ids:
            for prop in (
                db.query(Property)
                .filter(Property.object_type_id.in_(object_ids))
                .all()
            ):
                existing_props.setdefault(prop.object_type_id, {}).setdefault(
                    _prop_key(prop.name), prop
                )

        for item in properties:
            object_type_id = object_id_by_name.get(item.object_type_name)
            if not object_type_id:
                continue
            key = _prop_key(item.name)
            existing = existing_props.get(object_type_id, {}).get(key)
            incoming = {
                "display_name": item.display_name,
                "description": item.description,
                "data_type": item.data_type,
                "semantic_type": item.semantic_type,
            }
            if existing is None:
                prop = Property(
                    object_type_id=object_type_id,
                    name=item.name,
                    display_name=item.display_name,
                    description=item.description,
                    data_type=item.data_type,
                    semantic_type=item.semantic_type,
                    source_field_ref=item.source_field_ref,
                    required=item.required,
                    source_confidence=item.confidence,
                    sample_values_json=_dumps(item.sample_values) if item.sample_values else None,
                    unique_count=item.unique_count,
                    status=EntityStatus.SUGGESTED.value,
                    origin="machine",
                    machine_baseline=_dumps(incoming),
                    last_generation_id=gen_id,
                )
                db.add(prop)
                db.flush()
                # 登记到索引，防止同一批 properties 里的同名字段再插一份。
                existing_props.setdefault(object_type_id, {})[key] = prop
                report.record(
                    "properties", "added", prop.id, prop.name, prop.display_name
                )
            else:
                if existing.user_created:
                    existing.last_generation_id = gen_id
                    continue
                outcome, changed, conflicts = _merge_entity_fields(
                    existing, incoming, PROPERTY_FIELDS
                )
                # 跟随当前源：源字段引用可能随重命名/重摄取变化，更新为本次生成的值。
                existing.source_field_ref = item.source_field_ref
                existing.source_confidence = item.confidence
                # profiling 是机器数据，直接刷新（不参与三方合并的用户覆盖）。
                existing.sample_values_json = _dumps(item.sample_values) if item.sample_values else None
                existing.unique_count = item.unique_count
                existing.last_generation_id = gen_id
                existing.upstream_removed = False
                existing.origin = (
                    "machine_edited"
                    if _loads(existing.overridden_fields, [])
                    else "machine"
                )
                report.record(
                    "properties", outcome, existing.id, existing.name,
                    existing.display_name, fields=changed, conflicts=conflicts,
                )

        # 陈旧机器属性清理：本次生成处理过的对象下，未被本次刷新到的机器属性即陈旧/串台
        # 残留（旧命名批次遗留、跨表串入的字段），删除以免跨重跑累积。人工创建/编辑/已确认的保留。
        self._prune_stale_properties(db, object_ids, gen_id, report)

    def _prune_stale_properties(
        self,
        db: Session,
        object_ids: list[str],
        gen_id: str | None,
        report: MergeReport,
    ) -> None:
        if not object_ids or gen_id is None:
            return
        # 会话 autoflush=False：先把循环里对 last_generation_id 的更新刷入，
        # 否则下面的查询读到旧值、会把刚更新过的属性误判为陈旧而删除。
        db.flush()
        stale = (
            db.query(Property)
            .filter(
                Property.object_type_id.in_(object_ids),
                (Property.last_generation_id != gen_id)
                | (Property.last_generation_id.is_(None)),
            )
            .all()
        )
        for p in stale:
            if (
                p.user_created
                or _loads(p.overridden_fields, [])
                or p.status
                not in (EntityStatus.SUGGESTED.value, EntityStatus.DEPRECATED.value)
            ):
                continue  # 保留人工创建/编辑/已确认的属性
            report.record("properties", "removed", p.id, p.name, p.display_name)
            db.delete(p)

    def _handle_object_removal(
        self,
        db: Session,
        existing_objs: list[ObjectType],
        seen_object_ids: set[str],
        gen_id: str | None,
        report: MergeReport,
    ) -> None:
        for obj in existing_objs:
            if obj.id in seen_object_ids or obj.user_created or obj.deleted_by_user:
                continue
            has_edits = bool(_loads(obj.overridden_fields, [])) or obj.status not in (
                EntityStatus.SUGGESTED.value,
                EntityStatus.DEPRECATED.value,
            )
            referenced = (
                db.query(RelationType)
                .filter(
                    (RelationType.source_object_type_id == obj.id)
                    | (RelationType.target_object_type_id == obj.id)
                )
                .first()
                is not None
            )
            if has_edits or referenced:
                obj.status = EntityStatus.DEPRECATED.value
                obj.upstream_removed = True
                report.record(
                    "object_types", "removed", obj.id, obj.name, obj.display_name
                )
            else:
                report.record(
                    "object_types", "removed", obj.id, obj.name, obj.display_name
                )
                db.query(Property).filter(
                    Property.object_type_id == obj.id
                ).delete(synchronize_session=False)
                db.delete(obj)

    # ------------------------------------------------------------------
    # 关系
    # ------------------------------------------------------------------
    def merge_relations(
        self,
        db: Session,
        ontology_id: str,
        relation_types: list[DraftRelationType],
        resolve_object_id,
        gen_id: str | None,
        report: MergeReport,
        *,
        handle_removal: bool = False,
    ) -> int:
        """按 source_signature（回退 name）合并关系。

        ``resolve_object_id(name) -> id | None`` 把关系两端对象名解析为已入库
        ObjectType.id。两端有一端解析不到的关系跳过。
        """
        obj_by_id = {
            o.id: o
            for o in db.query(ObjectType)
            .filter(ObjectType.ontology_id == ontology_id)
            .all()
        }
        existing_rels = (
            db.query(RelationType).filter(RelationType.ontology_id == ontology_id).all()
        )
        existing_by_sig: dict[str, RelationType] = {}
        existing_by_name: dict[str, RelationType] = {}
        for rel in existing_rels:
            existing_by_name[rel.name] = rel
            sig = rel.source_signature or relation_signature(
                obj_by_id[rel.source_object_type_id].source_ref
                if rel.source_object_type_id in obj_by_id
                else None,
                obj_by_id[rel.target_object_type_id].source_ref
                if rel.target_object_type_id in obj_by_id
                else None,
                rel.structure_type,
            )
            if sig:
                existing_by_sig[sig] = rel

        written = 0
        seen_ids: set[str] = set()
        for item in relation_types:
            source_id = resolve_object_id(item.source_object_type_name)
            target_id = resolve_object_id(item.target_object_type_name)
            if not source_id or not target_id:
                continue
            # 桥表塌缩：承载该关系的关系表(bridge)实现表，回链为 mapping_object_type_id。
            # 解析不到（如实现表尚未入库）则留空，不阻塞关系本身入库。
            mapping_id = (
                resolve_object_id(item.mapping_object_type_name)
                if getattr(item, "mapping_object_type_name", None)
                else None
            )
            sig = relation_signature(
                obj_by_id[source_id].source_ref if source_id in obj_by_id else None,
                obj_by_id[target_id].source_ref if target_id in obj_by_id else None,
                item.structure_type,
            )
            existing = None
            if sig and sig in existing_by_sig:
                existing = existing_by_sig[sig]
            elif item.name in existing_by_name:
                existing = existing_by_name[item.name]

            incoming = {
                "display_name": item.display_name,
                "description": item.description,
                "cardinality": item.cardinality,
                "structure_type": item.structure_type,
            }
            if existing is None:
                rel = RelationType(
                    ontology_id=ontology_id,
                    name=item.name,
                    display_name=item.display_name,
                    description=item.description,
                    source_object_type_id=source_id,
                    target_object_type_id=target_id,
                    cardinality=item.cardinality,
                    structure_type=item.structure_type,
                    mapping_object_type_id=mapping_id,
                    source_evidence=item.source_evidence,
                    source_confidence=item.confidence,
                    source_signature=sig,
                    status=EntityStatus.SUGGESTED.value,
                    origin="machine",
                    machine_baseline=_dumps(incoming),
                    last_generation_id=gen_id,
                )
                db.add(rel)
                db.flush()
                report.record(
                    "relation_types", "added", rel.id, rel.name, rel.display_name
                )
                written += 1
                seen_ids.add(rel.id)
            else:
                if existing.user_created:
                    existing.last_generation_id = gen_id
                    seen_ids.add(existing.id)
                    written += 1
                    continue
                # 结构性字段（两端、证据、签名）始终跟随机器
                existing.source_object_type_id = source_id
                existing.target_object_type_id = target_id
                existing.mapping_object_type_id = mapping_id
                existing.source_evidence = item.source_evidence
                existing.source_confidence = item.confidence
                existing.source_signature = sig
                outcome, changed, conflicts = _merge_entity_fields(
                    existing, incoming, RELATION_FIELDS
                )
                existing.last_generation_id = gen_id
                existing.upstream_removed = False
                existing.origin = (
                    "machine_edited"
                    if _loads(existing.overridden_fields, [])
                    else "machine"
                )
                report.record(
                    "relation_types", outcome, existing.id, existing.name,
                    existing.display_name, fields=changed, conflicts=conflicts,
                )
                written += 1
                seen_ids.add(existing.id)

        if handle_removal:
            for rel in existing_rels:
                if rel.id in seen_ids or rel.user_created or rel.deleted_by_user:
                    continue
                has_edits = bool(_loads(rel.overridden_fields, [])) or rel.status not in (
                    EntityStatus.SUGGESTED.value,
                    EntityStatus.DEPRECATED.value,
                )
                report.record(
                    "relation_types", "removed", rel.id, rel.name, rel.display_name
                )
                if has_edits:
                    rel.status = EntityStatus.DEPRECATED.value
                    rel.upstream_removed = True
                else:
                    db.delete(rel)

        return written

    # ------------------------------------------------------------------
    # 全量合并入口
    # ------------------------------------------------------------------
    def merge_full(
        self,
        db: Session,
        ontology_id: str,
        draft: OntologyDraftOutput,
        gen_id: str | None,
    ) -> MergeReport:
        """完整草稿合并：对象/属性/关系三方合并，处理上游消失，刷新证据引用。

        业务逻辑（draft.business_logics）目前由生成器留空，故已有的人工/历史
        业务逻辑与绑定保持不变，不被再生成破坏。
        """
        report = MergeReport()
        object_ref_to_id = self.merge_objects(
            db, ontology_id, draft.object_types, draft.properties, gen_id, report,
            handle_removal=True,
        )
        # 关系两端在完整草稿里用的是 LLM 提升后的对象 name，按 name 回链。
        object_id_by_name = {
            o.name: o.id
            for o in db.query(ObjectType)
            .filter(ObjectType.ontology_id == ontology_id)
            .all()
        }
        self.merge_relations(
            db,
            ontology_id,
            draft.relation_types,
            lambda name: object_id_by_name.get(name),
            gen_id,
            report,
            handle_removal=True,
        )
        self._refresh_evidence(db, ontology_id, draft.evidence_refs)
        return report

    @staticmethod
    def _refresh_evidence(
        db: Session, ontology_id: str, evidence_refs: list[str]
    ) -> None:
        existing = {
            e.source_ref
            for e in db.query(DraftEvidence)
            .filter(DraftEvidence.ontology_id == ontology_id)
            .all()
        }
        for ref in evidence_refs:
            if ref in existing:
                continue
            db.add(
                DraftEvidence(
                    ontology_id=ontology_id,
                    evidence_type="datahub_ref",
                    source_ref=ref,
                    payload_summary=ref,
                    confidence=0.5,
                )
            )
