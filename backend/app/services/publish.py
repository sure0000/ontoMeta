import json
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import (
    BusinessLogic,
    BusinessLogicObjectBinding,
    BusinessLogicPropertyBinding,
    ChangeConfirmation,
    ConfirmationStatus,
    DraftEvidence,
    EntityStatus,
    ObjectType,
    Ontology,
    OntologyStatus,
    Property,
    RelationType,
    VersionRecord,
)
from app.schemas import (
    ConfirmationCreate,
    DraftObjectType,
    DraftProperty,
    DraftRelationType,
    OntologyDraftOutput,
)
from app.services.common import log_change
from app.services.ontology_merge import seed_published_authority

logger = logging.getLogger("ontometa.publish")
from app.services.version_diff import (
    capture_ontology_snapshot,
    compute_version_diff,
    load_previous_snapshot,
    summarize_diff,
)

def _log_change(
    db: Session,
    entity_type: str,
    entity_id: str,
    action: str,
    operator: str | None = None,
    summary: str | None = None,
) -> None:
    log_change(db, entity_type, entity_id, action, operator, summary)


def _rebuild_semantic_index(db: Session, ontology_id: str) -> None:
    """发布后重建语义检索索引（P1.5）。

    挂在发布这一刻，是因为**只有已发布内容可被 Agent 检索**——索引与可检索集必须同步，
    否则语义检索会召回一个查得到名字、`get_object` 却拿不到的实体。

    绝不阻断发布：未配置嵌入服务时它本就返回 0；调用失败也只记日志。
    发布是不可逆的治理动作，不能因为一个检索增强而失败。
    """
    try:
        from app.services.semantic_search import build_index

        n = build_index(db, ontology_id)
        if n:
            logger.info("语义索引已重建：ontology=%s，%d 条", ontology_id, n)
    except Exception as exc:  # noqa: BLE001
        logger.warning("语义索引重建失败（不影响发布）：%s", exc)


class DraftPersistenceService:
    """持久化草稿与证据引用。"""

    def save_draft(
        self,
        db: Session,
        ontology: Ontology,
        draft: OntologyDraftOutput,
    ) -> Ontology:
        object_name_to_id: dict[str, str] = {}
        # object_type_name -> { field_name -> property_id }
        object_field_to_property_id: dict[str, dict[str, str]] = {}

        object_models: list[tuple[str, ObjectType]] = []
        for item in draft.object_types:
            obj = ObjectType(
                ontology_id=ontology.id,
                name=item.name,
                display_name=item.display_name,
                description=item.description,
                source_ref=item.source_ref,
                source_confidence=item.confidence,
                table_role=item.table_role,
                role_confidence=item.role_confidence,
                role_reason=item.role_reason,
                needs_review=item.needs_review,
                role_signals=(
                    json.dumps(item.role_signals, ensure_ascii=False)
                    if item.role_signals is not None
                    else None
                ),
                status=EntityStatus.SUGGESTED.value,
            )
            db.add(obj)
            object_models.append((item.name, obj))
        db.flush()
        for name, obj in object_models:
            object_name_to_id[name] = obj.id
            object_field_to_property_id[name] = {}

        property_models: list[tuple[str, str, Property]] = []
        for item in draft.properties:
            object_type_id = object_name_to_id.get(item.object_type_name)
            if not object_type_id:
                continue
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
                status=EntityStatus.SUGGESTED.value,
            )
            db.add(prop)
            property_models.append((item.object_type_name, item.name, prop))
        if property_models:
            db.flush()
            for object_type_name, field_name, prop in property_models:
                object_field_to_property_id.setdefault(object_type_name, {})[
                    field_name
                ] = prop.id

        for item in draft.relation_types:
            source_id = object_name_to_id.get(item.source_object_type_name)
            target_id = object_name_to_id.get(item.target_object_type_name)
            if not source_id or not target_id:
                continue
            mapping_id = (
                object_name_to_id.get(item.mapping_object_type_name)
                if item.mapping_object_type_name
                else None
            )
            db.add(
                RelationType(
                    ontology_id=ontology.id,
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
                    status=EntityStatus.SUGGESTED.value,
                )
            )

        logic_name_to_id: dict[str, str] = {}
        logic_models: list[tuple[str, BusinessLogic]] = []
        for item in draft.business_logics:
            logic = BusinessLogic(
                ontology_id=ontology.id,
                name=item.name,
                display_name=item.display_name,
                logic_type=item.logic_type,
                description=item.description,
                expression_summary=item.expression_summary,
                source_type=item.source_type,
                source_ref=item.source_ref,
                source_confidence=item.confidence,
                status=EntityStatus.SUGGESTED.value,
            )
            db.add(logic)
            logic_models.append((item.name, logic))
        if logic_models:
            db.flush()
            for name, logic in logic_models:
                logic_name_to_id[name] = logic.id

        for item in draft.business_logic_object_bindings:
            logic_id = logic_name_to_id.get(item.logic_name)
            object_type_id = object_name_to_id.get(item.object_type_name)
            if not logic_id or not object_type_id:
                continue
            db.add(
                BusinessLogicObjectBinding(
                    business_logic_id=logic_id,
                    object_type_id=object_type_id,
                    role=item.role,
                    source="inferred",
                    confidence=item.confidence,
                )
            )

        for item in draft.business_logic_property_bindings:
            logic_id = logic_name_to_id.get(item.logic_name)
            property_id = object_field_to_property_id.get(
                item.object_type_name, {}
            ).get(item.field_name)
            if not logic_id or not property_id:
                continue
            db.add(
                BusinessLogicPropertyBinding(
                    business_logic_id=logic_id,
                    property_id=property_id,
                    role=item.role,
                    source="inferred",
                    confidence=item.confidence,
                )
            )

        for ref in draft.evidence_refs:
            db.add(
                DraftEvidence(
                    ontology_id=ontology.id,
                    evidence_type="datahub_ref",
                    source_ref=ref,
                    payload_summary=ref,
                    confidence=0.5,
                )
            )

        ontology.generated_at = datetime.now(timezone.utc)
        ontology.status = OntologyStatus.DRAFT.value
        db.commit()
        db.refresh(ontology)
        return ontology

    def upsert_objects(
        self,
        db: Session,
        ontology: Ontology,
        object_types: list[DraftObjectType],
        properties: list[DraftProperty],
    ) -> dict[str, str]:
        """按 source_ref(数据集 urn) upsert 对象与属性到已有草稿本体。

        不删除本体下已有的关系，也不删除评估中已消失的对象/属性——用于
        「仅生成业务对象」独立执行，可与「仅生成业务关系」并行，互不清空
        对方产出。返回 source_ref -> object_type_id，供关系生成按 urn 精确回链。
        """
        existing_by_ref: dict[str, ObjectType] = {
            obj.source_ref: obj
            for obj in db.query(ObjectType)
            .filter(ObjectType.ontology_id == ontology.id)
            .all()
            if obj.source_ref
        }

        object_ref_to_id: dict[str, str] = {}
        object_id_by_name: dict[str, str] = {}
        for item in object_types:
            existing = existing_by_ref.get(item.source_ref) if item.source_ref else None
            if existing is not None:
                existing.name = item.name
                existing.display_name = item.display_name
                existing.description = item.description
                existing.source_confidence = item.confidence
                existing.table_role = item.table_role
                existing.role_confidence = item.role_confidence
                existing.role_reason = item.role_reason
                obj = existing
            else:
                obj = ObjectType(
                    ontology_id=ontology.id,
                    name=item.name,
                    display_name=item.display_name,
                    description=item.description,
                    source_ref=item.source_ref,
                    source_confidence=item.confidence,
                    table_role=item.table_role,
                    role_confidence=item.role_confidence,
                    role_reason=item.role_reason,
                    status=EntityStatus.SUGGESTED.value,
                )
                db.add(obj)
            db.flush()
            if item.source_ref:
                object_ref_to_id[item.source_ref] = obj.id
            object_id_by_name[item.name] = obj.id

        existing_props_by_object: dict[str, dict[str, Property]] = {}
        if object_id_by_name:
            for prop in (
                db.query(Property)
                .filter(Property.object_type_id.in_(list(object_id_by_name.values())))
                .all()
            ):
                existing_props_by_object.setdefault(prop.object_type_id, {})[
                    prop.source_field_ref or prop.name
                ] = prop

        for item in properties:
            object_type_id = object_id_by_name.get(item.object_type_name)
            if not object_type_id:
                continue
            key = item.source_field_ref or item.name
            existing_prop = existing_props_by_object.get(object_type_id, {}).get(key)
            if existing_prop is not None:
                existing_prop.name = item.name
                existing_prop.display_name = item.display_name
                existing_prop.description = item.description
                existing_prop.data_type = item.data_type
                existing_prop.semantic_type = item.semantic_type
                existing_prop.source_field_ref = item.source_field_ref
                existing_prop.required = item.required
                existing_prop.source_confidence = item.confidence
            else:
                db.add(
                    Property(
                        object_type_id=object_type_id,
                        name=item.name,
                        display_name=item.display_name,
                        description=item.description,
                        data_type=item.data_type,
                        semantic_type=item.semantic_type,
                        source_field_ref=item.source_field_ref,
                        required=item.required,
                        source_confidence=item.confidence,
                        status=EntityStatus.SUGGESTED.value,
                    )
                )

        ontology.generated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(ontology)
        return object_ref_to_id

    def upsert_relations(
        self,
        db: Session,
        ontology: Ontology,
        relation_types: list[DraftRelationType],
        object_id_by_candidate: dict[str, str],
    ) -> int:
        """按 name upsert 关系类型到已有草稿本体，不触碰该本体的对象/属性。

        ``relation_types`` 的 source/target 对象名是证据 candidate_name(未经
        业务命名提升，见 ``OntologyDraftGenerator.generate_relations``)；
        ``object_id_by_candidate`` 由调用方按 source_dataset_urn 把 candidate_name
        回链到已入库的 ObjectType.id。两端有一端回链不到(如对象尚未生成)的
        关系会被跳过，不计入返回的已写入数量。
        """
        existing_by_name = {
            rel.name: rel
            for rel in db.query(RelationType)
            .filter(RelationType.ontology_id == ontology.id)
            .all()
        }

        written = 0
        for item in relation_types:
            source_id = object_id_by_candidate.get(item.source_object_type_name)
            target_id = object_id_by_candidate.get(item.target_object_type_name)
            if not source_id or not target_id:
                continue
            mapping_id = (
                object_id_by_candidate.get(item.mapping_object_type_name)
                if item.mapping_object_type_name
                else None
            )
            existing = existing_by_name.get(item.name)
            if existing is not None:
                existing.display_name = item.display_name
                existing.description = item.description
                existing.source_object_type_id = source_id
                existing.target_object_type_id = target_id
                existing.cardinality = item.cardinality
                existing.structure_type = item.structure_type
                existing.mapping_object_type_id = mapping_id
                existing.source_evidence = item.source_evidence
                existing.source_confidence = item.confidence
            else:
                db.add(
                    RelationType(
                        ontology_id=ontology.id,
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
                        status=EntityStatus.SUGGESTED.value,
                    )
                )
            written += 1

        ontology.generated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(ontology)
        return written


class PublishSelection:
    """一次发布会提升哪些实体、又会跳过多少——发布与发布前自检共用同一份判定。"""

    def __init__(self) -> None:
        self.entities: list = []
        self.object_count = 0
        self.property_count = 0
        self.relation_count = 0
        self.skipped_needs_review = 0
        self.skipped_non_business = 0
        self.skipped_relation_endpoint = 0


class PublishService:
    """将编辑确认后的草稿发布为正式版本。"""

    def select_publishable(self, db: Session, ontology_id: str) -> PublishSelection:
        """部分发布的判定：只发布「needs_review=false 的业务对象 + 其属性 + 两端都落在
        该集合里的业务关系」。待复核对象、其它角色对象（数据表/关系表/技术表）、端点
        未发布的关系一律保持原状。业务逻辑仍走 publish_business_logic 单独发布。

        发布与发布前自检共用这一份判定——两边各写一遍必然漂移，而漂移的表现就是
        「预检说要发 128 个，发完却是 0 个」。
        """
        sel = PublishSelection()
        objects = (
            db.query(ObjectType)
            .filter(ObjectType.ontology_id == ontology_id)
            .all()
        )
        business_objects = [o for o in objects if o.table_role == "business_object"]
        sel.skipped_non_business = len(objects) - len(business_objects)
        confirmed = [
            o
            for o in business_objects
            if not o.needs_review and not getattr(o, "deleted_by_user", False)
        ]
        sel.skipped_needs_review = len(business_objects) - len(confirmed)
        sel.entities.extend(confirmed)
        sel.object_count = len(confirmed)

        confirmed_ids = {o.id for o in confirmed}
        if confirmed_ids:
            props = (
                db.query(Property)
                .filter(Property.object_type_id.in_(confirmed_ids))
                .all()
            )
            sel.entities.extend(props)
            sel.property_count = len(props)

        relations = (
            db.query(RelationType)
            .filter(RelationType.ontology_id == ontology_id)
            .all()
        )
        for r in relations:
            if getattr(r, "deleted_by_user", False) or r.status == EntityStatus.DEPRECATED.value:
                continue
            if (
                r.source_object_type_id in confirmed_ids
                and r.target_object_type_id in confirmed_ids
            ):
                sel.entities.append(r)
                sel.relation_count += 1
            else:
                sel.skipped_relation_endpoint += 1
        return sel

    def preflight(self, db: Session, ontology_id: str) -> dict:
        """发布前自检：把「将发布什么、将跳过什么、为什么」在点之前算给用户看。

        存在的理由是一个真实故障模式——源库无主键导致对象 100% 被打上待复核，
        发布提升 0 个、本体浏览页一片空白，而用户只看到一句「发布成功」。

        S4 发布门禁：检测孤点——即将发布的对象若一跳邻居都未发布，发布后就是孤点。
        """
        ontology = db.get(Ontology, ontology_id)
        if not ontology:
            raise ValueError("Ontology not found")
        sel = self.select_publishable(db, ontology_id)
        conflicts = 0
        for model in (ObjectType, RelationType):
            conflicts += (
                db.query(model)
                .filter(
                    model.ontology_id == ontology_id,
                    model.conflict_json.isnot(None),
                )
                .count()
            )

        # S4: 孤点检测——即将发布的对象，若其一跳邻居都不在发布集合中，就是孤点
        isolated_objects = self._detect_isolated_objects(db, ontology_id, sel)

        return {
            "ontology_id": ontology_id,
            "current_version": ontology.version,
            "next_version": ontology.version + 1,
            "object_count": sel.object_count,
            "property_count": sel.property_count,
            "relation_count": sel.relation_count,
            "skipped_needs_review": sel.skipped_needs_review,
            "skipped_non_business": sel.skipped_non_business,
            "skipped_relation_endpoint": sel.skipped_relation_endpoint,
            "unresolved_conflicts": conflicts,
            "isolated_objects": isolated_objects,
        }

    def _detect_isolated_objects(
        self, db: Session, ontology_id: str, selection: PublishSelection
    ) -> list[dict]:
        """检测即将发布的对象中，哪些会成为孤点（S4）。

        孤点定义：对象在草稿中有关系，但这些关系的另一端都不在发布集合中；
        或者对象根本没有任何关系连接。
        """
        publishable_object_ids = {
            obj.id for obj in selection.entities if isinstance(obj, ObjectType)
        }
        if not publishable_object_ids:
            return []

        # 查询草稿中所有涉及这些对象的关系（包括不会被发布的关系）
        all_relations = (
            db.query(RelationType)
            .filter(
                RelationType.ontology_id == ontology_id,
                (
                    RelationType.source_object_type_id.in_(publishable_object_ids)
                    | RelationType.target_object_type_id.in_(publishable_object_ids)
                ),
            )
            .all()
        )

        # 构建邻接表：object_id -> set of neighbor_ids
        neighbors_map: dict[str, set[str]] = {oid: set() for oid in publishable_object_ids}
        for rel in all_relations:
            if rel.source_object_type_id in publishable_object_ids:
                neighbors_map[rel.source_object_type_id].add(rel.target_object_type_id)
            if rel.target_object_type_id in publishable_object_ids:
                neighbors_map[rel.target_object_type_id].add(rel.source_object_type_id)

        # 检测孤点：没有邻居，或所有邻居都不在发布集合中
        isolated = []
        for obj in selection.entities:
            if not isinstance(obj, ObjectType):
                continue
            neighbors = neighbors_map.get(obj.id, set())
            published_neighbors = neighbors & publishable_object_ids

            if not neighbors:
                # 根本没有关系连接
                isolated.append({
                    "object_id": obj.id,
                    "object_name": obj.display_name or obj.name,
                    "reason": "no_relations",
                    "unpublished_neighbor_count": 0,
                })
            elif not published_neighbors:
                # 有关系，但所有邻居都不在发布集合中
                isolated.append({
                    "object_id": obj.id,
                    "object_name": obj.display_name or obj.name,
                    "reason": "all_neighbors_unpublished",
                    "unpublished_neighbor_count": len(neighbors),
                })

        return isolated

    def publish(self, db: Session, ontology_id: str, operator: str | None = None) -> Ontology:
        ontology = db.get(Ontology, ontology_id)
        if not ontology:
            raise ValueError("Ontology not found")

        # 形式化不变式校验（F2）：``formal_enforcement=error`` 时，error 级违反阻断发布。
        # warn/off 不阻断（迁移期安全）；这与发布后才能供 Data Agent 查询的时序契合——
        # 不让不可推理的本体进入已发布集。
        from app.config import settings as _env_settings
        from app.services.ontology_formal import assert_publishable

        assert_publishable(
            db, ontology_id, getattr(_env_settings, "formal_enforcement", "warn")
        )

        # 一域一发布：域内绝不允许出现第二个 published 本体行。历史上发布后再生成会
        # 新建一行、再发布就攒出两个 published——本体浏览页与 Agent 可检索集一起翻倍，
        # 版本历史还会整段丢失。这里当场拦住，把用户导回那一行工作本体。
        sibling = (
            db.query(Ontology)
            .filter(
                Ontology.domain_context_id == ontology.domain_context_id,
                Ontology.status == OntologyStatus.PUBLISHED.value,
                Ontology.id != ontology.id,
            )
            .first()
        )
        if sibling is not None:
            raise ValueError(
                f"该数据域已有已发布本体（v{sibling.version}），不能再发布第二个。"
                "请在该数据域的工作本体上继续编辑后发布。"
            )

        new_version = ontology.version + 1
        previous_snapshot = load_previous_snapshot(
            db, ontology_id, before_version=new_version
        )
        current_snapshot = capture_ontology_snapshot(db, ontology_id)
        diff = compute_version_diff(previous_snapshot, current_snapshot)
        diff_summary = f"发布本体版本 v{new_version}：{summarize_diff(diff)}"

        ontology.version = new_version
        ontology.status = OntologyStatus.PUBLISHED.value
        ontology.published_at = datetime.now(timezone.utc)
        ontology.approved_by = operator

        selection = self.select_publishable(db, ontology_id)
        entities: list = selection.entities

        # 提升状态的同时把结构性字段升为人工权威：一域一本体后再生成直接打在这一行上，
        # 没有这一步，人没手动改过的已发布字段会被机器静默覆盖（详见 seed_published_authority）。
        seeded = 0
        for entity in entities:
            if entity.status != EntityStatus.DEPRECATED.value:
                entity.status = EntityStatus.PUBLISHED.value
                # 本次发布把改动固化进新版本，「待固化」凭据随之清零。
                if hasattr(entity, "has_unpublished_change"):
                    entity.has_unpublished_change = False
                if seed_published_authority(entity):
                    seeded += 1
        if seeded:
            logger.info(
                "发布固化人工权威：ontology=%s，%d 个实体的结构性字段已钉住",
                ontology.id,
                seeded,
            )

        db.add(
            VersionRecord(
                entity_type="ontology",
                entity_id=ontology.id,
                version=ontology.version,
                diff_summary=diff_summary,
                diff_json=json.dumps(diff, ensure_ascii=False),
                snapshot_json=json.dumps(current_snapshot, ensure_ascii=False),
                operator=operator,
            )
        )
        _log_change(db, "ontology", ontology.id, "publish", operator, f"v{ontology.version}")
        db.commit()
        db.refresh(ontology)
        _rebuild_semantic_index(db, ontology.id)
        return ontology

    def publish_business_logic(
        self, db: Session, logic_id: str, operator: str | None = None
    ) -> BusinessLogic:
        """发布单条业务逻辑:置为 published,引用绑定即固化为与已发布本体的正式绑定。"""
        logic = db.get(BusinessLogic, logic_id)
        if not logic:
            raise ValueError("Business logic not found")

        logic.status = EntityStatus.PUBLISHED.value
        # 版本号沿用其所属本体当前版本,作为该逻辑的发布快照记录
        ontology = db.get(Ontology, logic.ontology_id)
        version = ontology.version if ontology else 0
        db.add(
            VersionRecord(
                entity_type="business_logic",
                entity_id=logic.id,
                version=version,
                diff_summary=f"发布业务逻辑:{logic.display_name}",
                operator=operator,
            )
        )
        _log_change(db, "business_logic", logic.id, "publish", operator, "发布业务逻辑")
        db.commit()
        db.refresh(logic)
        _rebuild_semantic_index(db, logic.ontology_id)
        return logic


class ConfirmationService:
    """重要操作二次确认。"""

    def __init__(self) -> None:
        self.publish_service = PublishService()

    def create(self, db: Session, data: ConfirmationCreate) -> ChangeConfirmation:
        confirmation = ChangeConfirmation(
            ontology_id=data.ontology_id,
            target_type=data.target_type,
            target_id=data.target_id,
            action_type=data.action_type,
            operator=data.operator,
            reason=data.reason,
            payload=json.dumps(data.payload, ensure_ascii=False) if data.payload else None,
            confirmation_status=ConfirmationStatus.PENDING.value,
        )
        db.add(confirmation)
        db.commit()
        db.refresh(confirmation)
        return confirmation

    def get(self, db: Session, confirmation_id: str) -> ChangeConfirmation | None:
        return db.get(ChangeConfirmation, confirmation_id)

    def confirm(
        self, db: Session, confirmation_id: str, operator: str | None = None
    ) -> ChangeConfirmation:
        confirmation = db.get(ChangeConfirmation, confirmation_id)
        if not confirmation:
            raise ValueError("Confirmation not found")
        if confirmation.confirmation_status != ConfirmationStatus.PENDING.value:
            raise ValueError("Confirmation is not pending")

        confirmation.confirmation_status = ConfirmationStatus.CONFIRMED.value
        confirmation.confirmed_at = datetime.now(timezone.utc)
        if operator:
            confirmation.operator = operator

        if confirmation.action_type == "publish":
            if confirmation.target_type == "business_logic" and confirmation.target_id:
                self.publish_service.publish_business_logic(
                    db, confirmation.target_id, confirmation.operator
                )
            else:
                self.publish_service.publish(db, confirmation.ontology_id, confirmation.operator)
        elif confirmation.action_type == "delete" and confirmation.target_id:
            if confirmation.target_type == "business_logic":
                logic = db.get(BusinessLogic, confirmation.target_id)
                if logic:
                    db.delete(logic)
            _log_change(
                db,
                confirmation.target_type,
                confirmation.target_id,
                "delete",
                confirmation.operator,
            )

        db.commit()
        db.refresh(confirmation)
        return confirmation

    def cancel(self, db: Session, confirmation_id: str) -> ChangeConfirmation:
        confirmation = db.get(ChangeConfirmation, confirmation_id)
        if not confirmation:
            raise ValueError("Confirmation not found")
        if confirmation.confirmation_status != ConfirmationStatus.PENDING.value:
            raise ValueError("Confirmation is not pending")
        confirmation.confirmation_status = ConfirmationStatus.CANCELLED.value
        db.commit()
        db.refresh(confirmation)
        return confirmation
