import json

from sqlalchemy.orm import Session

from app.models import (
    BusinessLogic,
    BusinessLogicCategory,
    BusinessLogicObjectBinding,
    BusinessLogicPropertyBinding,
    DomainContext,
    EntityStatus,
    ObjectType,
    Ontology,
    Property,
    RelationType,
)
from app.services.relation_terms import compact_relation_term, validate_relation_term
from app.services.common import log_change
from app.ontology_types import is_valid_cardinality, is_valid_semantic_type
from app.schemas import (
    BusinessLogicDetail,
    BusinessLogicObjectBindingOut,
    BusinessLogicPropertyBindingOut,
    BusinessLogicOut,
    ObjectTypeDetail,
    ObjectTypeSummary,
    PropertyOut,
    RelationTypeOut,
)

_OBJECT_BINDING_ROLES = {"subject", "dimension", "output"}
_PROPERTY_BINDING_ROLES = {"input", "output", "filter", "group"}

# 对象角色的合法取值（与前端 ROLE_META 一致）。
_ALLOWED_TABLE_ROLES = {"business_object", "data_table", "bridge", "technical"}


def _assert_object_name_free(
    db: Session, ontology_id: str, name: str, *, exclude_id: str | None = None
) -> None:
    """对象标识名在本体内必须唯一（对应 ``uq_object_type_ontology_name``）。

    库层有唯一约束兜底，但那只会抛 IntegrityError（→ 500）。在这里先查一次，
    让用户拿到「叫什么、跟谁撞了」的可行动提示，而不是一句约束违反。
    """
    query = db.query(ObjectType).filter(
        ObjectType.ontology_id == ontology_id,
        ObjectType.name == name,
    )
    if exclude_id is not None:
        query = query.filter(ObjectType.id != exclude_id)
    clash = query.first()
    if clash is not None:
        raise ValueError(
            f"对象标识名「{name}」在本体内已被「{clash.display_name}」占用，请换一个"
        )


def _formal_enforced() -> bool:
    """当前是否强制形式化枚举校验（formal_enforcement=error）。warn/off 不阻断编辑。"""
    from app.config import settings

    return (getattr(settings, "formal_enforcement", "warn") or "warn").lower() == "error"


def _validate_cardinality_or_raise(value: str | None) -> None:
    if value is None or not _formal_enforced():
        return
    if not is_valid_cardinality(value):
        raise ValueError(
            f"非法基数「{value}」：须为 one_to_one/one_to_many/many_to_one/many_to_many 之一"
        )


def _validate_semantic_type_or_raise(value: str | None) -> None:
    if value is None or not _formal_enforced():
        return
    if not is_valid_semantic_type(value):
        raise ValueError(
            f"非法语义类型「{value}」：须为 identifier/measure/temporal/categorical/textual/technical 之一"
        )


def _set_review_mark(obj: ObjectType, needs_review: bool) -> None:
    """置复核状态。独立布尔列，不再动 role_reason——那是描述性字段，归机器刷新。"""
    obj.needs_review = bool(needs_review)


def _log_change(
    db: Session,
    entity_type: str,
    entity_id: str,
    action: str,
    operator: str | None = None,
    summary: str | None = None,
) -> None:
    log_change(db, entity_type, entity_id, action, operator, summary)


def _mark_edited(entity) -> None:
    """人工编辑后的实体状态。

    A 案（见 docs/ONTOLOGY_LIFECYCLE_REDESIGN.md §3.4）：**人工即权威，改了立即生效**。
    已发布实体被编辑后仍保持 published，不再退回 edited——旧行为会让「只是改了个中文
    名」把该对象从对外可见集里静默摘掉，直到用户再点一次发布才回来，界面上毫无提示。
    机器改动才需要过闸（三方合并的冲突通道），人不需要。

    发布这一刻仍有意义：打版本快照 + 提升新确认的实体。已发布内容被直接改动会由
    「N 项待固化」提示条呈现，见 workspace_service 的 unpublished_change_count。
    """
    if entity.status == EntityStatus.PUBLISHED.value:
        # 已发布内容被直接改动：状态不动（立即生效），但要留下「待固化」凭据，
        # 否则这件事在界面上完全不可见。publish() 提升实体时清零。
        if hasattr(entity, "has_unpublished_change"):
            entity.has_unpublished_change = True
        return
    if entity.status == EntityStatus.PRE_PUBLISHED.value:
        return
    entity.status = EntityStatus.EDITED.value


def _mark_overridden(entity, fields: list[str]) -> None:
    """把被人工修改的字段钉住（计入 overridden_fields），并标记来源。

    同时清理这些字段的待复核冲突（人工直接编辑即视为已解决）。
    仅对具备溯源字段的实体生效（ObjectType/Property/RelationType/BusinessLogic）。
    """
    if not hasattr(entity, "overridden_fields"):
        return
    current = set(json.loads(entity.overridden_fields) if entity.overridden_fields else [])
    current.update(fields)
    entity.overridden_fields = json.dumps(sorted(current), ensure_ascii=False)
    entity.origin = "manual" if getattr(entity, "user_created", False) else "machine_edited"
    if getattr(entity, "conflict_json", None):
        try:
            conflicts = json.loads(entity.conflict_json)
        except (TypeError, json.JSONDecodeError):
            conflicts = {}
        for f in fields:
            conflicts.pop(f, None)
        entity.conflict_json = (
            json.dumps(conflicts, ensure_ascii=False) if conflicts else None
        )


class EditService:
    """工作区本体编辑与预发布。"""

    def __init__(self) -> None:
        # 延迟加载以避免循环导入；OntologyQueryService 无状态，可安全缓存于实例。
        self._query_service = None

    @property
    def query(self):
        if self._query_service is None:
            from app.services.query import OntologyQueryService

            self._query_service = OntologyQueryService()
        return self._query_service

    def update_object_type(
        self,
        db: Session,
        object_type_id: str,
        *,
        name: str | None = None,
        display_name: str | None = None,
        description: str | None = None,
        table_role: str | None = None,
        needs_review: bool | None = None,
        operator: str | None = None,
    ) -> ObjectTypeDetail:
        obj = db.get(ObjectType, object_type_id)
        if not obj:
            raise ValueError("Object type not found")

        changed: list[str] = []
        if name is not None:
            if name != obj.name:
                _assert_object_name_free(db, obj.ontology_id, name, exclude_id=obj.id)
            obj.name = name
            changed.append("name")
        if display_name is not None:
            obj.display_name = display_name
            changed.append("display_name")
        if description is not None:
            obj.description = description
            changed.append("description")

        role_confirmed = False
        if table_role is not None and table_role != obj.table_role:
            if table_role not in _ALLOWED_TABLE_ROLES:
                raise ValueError(f"非法对象角色：{table_role}")
            obj.table_role = table_role
            changed.append("table_role")
            # 人工改判角色即视为复核通过。
            role_confirmed = True

        # 复核状态：显式 needs_review 优先；否则改角色时自动置为已确认。
        # 它是独立列，**不计入 changed**——changed 会被 _mark_overridden 永久钉住，
        # 而复核与 role_reason 描述文本无关，钉住它等于让机器再也刷新不了角色依据。
        review_changed = False
        if needs_review is not None:
            _set_review_mark(obj, needs_review)
            review_changed = True
        elif role_confirmed:
            _set_review_mark(obj, False)
            review_changed = True

        if changed:
            _mark_overridden(obj, changed)
        elif not review_changed:
            return self._object_detail_or_raise(db, object_type_id)

        _mark_edited(obj)

        _log_change(db, "object_type", obj.id, "edit", operator, "更新对象类型")
        db.commit()

        return self._object_detail_or_raise(db, object_type_id)

    def _object_detail_or_raise(self, db: Session, object_type_id: str) -> ObjectTypeDetail:
        detail = self.query.get_object_type(db, object_type_id)
        if not detail:
            raise ValueError("Object type not found")
        return detail

    def batch_update_object_types(
        self,
        db: Session,
        ids: list[str],
        *,
        table_role: str | None = None,
        needs_review: bool | None = None,
        operator: str | None = None,
    ) -> list[ObjectTypeSummary]:
        """批量改判对象角色(table_role)与复核状态(needs_review)。

        与单条 update_object_type 语义一致：改角色即视为复核通过（自动清除
        needs_review=False），显式 needs_review 优先。跳过不存在或无实际变更的 id，
        一次性提交，返回已更新对象的摘要。
        """
        if table_role is not None and table_role not in _ALLOWED_TABLE_ROLES:
            raise ValueError(f"非法对象角色：{table_role}")
        if needs_review is None and table_role is None:
            return []
        ids = [i for i in ids if i]
        if not ids:
            return []

        objs = db.query(ObjectType).filter(ObjectType.id.in_(ids)).all()
        updated: list[ObjectType] = []
        for obj in objs:
            changed: list[str] = []
            role_confirmed = False
            if table_role is not None and table_role != obj.table_role:
                obj.table_role = table_role
                changed.append("table_role")
                role_confirmed = True

            # 复核状态是独立列，不计入 changed（同 update_object_type）。
            review_changed = False
            if needs_review is not None and bool(needs_review) != bool(obj.needs_review):
                _set_review_mark(obj, needs_review)
                review_changed = True
            elif needs_review is None and role_confirmed and obj.needs_review:
                _set_review_mark(obj, False)
                review_changed = True

            if not changed and not review_changed:
                continue
            if changed:
                _mark_overridden(obj, changed)
            _mark_edited(obj)
            _log_change(db, "object_type", obj.id, "edit", operator, "批量更新对象类型")
            updated.append(obj)

        db.commit()
        return [self.query._to_object_summary(db, o) for o in updated]

    async def ensure_object_type_from_dataset(
        self,
        db: Session,
        ontology_id: str,
        dataset_urn: str,
        *,
        operator: str | None = None,
    ) -> ObjectTypeSummary:
        """根据 DataHub dataset urn 查找或创建对应 ObjectType（用于关系承载表）。

        - 已存在同 source_ref 的 ObjectType：直接返回
        - 不存在：从 DataHub（或 mock）拉取 dataset 元数据，创建 ObjectType
        """
        from app.connectors.datahub import DataHubConnector
        from app.services.evidence_builder import _infer_object_name
        from app.services.settings_service import SettingsService

        ontology = db.get(Ontology, ontology_id)
        if not ontology:
            raise ValueError("Ontology not found")

        existing = (
            db.query(ObjectType)
            .filter(
                ObjectType.ontology_id == ontology_id,
                ObjectType.source_ref == dataset_urn,
            )
            .first()
        )
        if existing:
            return self.query._to_object_summary(db, existing)

        connector = DataHubConnector(SettingsService().get_datahub_runtime(db))
        try:
            dataset = await connector.get_dataset_by_urn(dataset_urn)
        finally:
            await connector.aclose()

        object_name = _infer_object_name(dataset.name)
        candidate_name = object_name
        suffix = 1
        while (
            db.query(ObjectType)
            .filter(
                ObjectType.ontology_id == ontology_id,
                ObjectType.name == candidate_name,
            )
            .first()
        ):
            suffix += 1
            candidate_name = f"{object_name}_{suffix}"

        obj = ObjectType(
            ontology_id=ontology_id,
            name=candidate_name,
            display_name=dataset.display_name or dataset.name,
            description=dataset.description,
            source_ref=dataset.urn,
            source_confidence=0.5,
            status=EntityStatus.SUGGESTED.value,
            user_created=True,
            origin="manual",
        )
        db.add(obj)
        db.flush()
        _log_change(
            db,
            "object_type",
            obj.id,
            "create",
            operator,
            f"从 DataHub dataset 创建承载表对象：{dataset.name}",
        )
        db.commit()
        db.refresh(obj)
        return self.query._to_object_summary(db, obj)

    def update_property(
        self,
        db: Session,
        property_id: str,
        *,
        display_name: str | None = None,
        description: str | None = None,
        data_type: str | None = None,
        semantic_type: str | None = None,
        operator: str | None = None,
    ) -> PropertyOut:
        prop = db.get(Property, property_id)
        if not prop:
            raise ValueError("Property not found")

        changed: list[str] = []
        if display_name is not None:
            prop.display_name = display_name
            changed.append("display_name")
        if description is not None:
            prop.description = description
            changed.append("description")
        if data_type is not None:
            prop.data_type = data_type
            changed.append("data_type")
        if semantic_type is not None:
            _validate_semantic_type_or_raise(semantic_type)
            prop.semantic_type = semantic_type
            changed.append("semantic_type")
        if changed:
            _mark_overridden(prop, changed)

        _mark_edited(prop)

        obj = db.get(ObjectType, prop.object_type_id)
        if obj:
            _mark_edited(obj)

        _log_change(db, "property", prop.id, "edit", operator, "更新属性")
        db.commit()
        db.refresh(prop)
        return PropertyOut.model_validate(prop)

    def create_relation_type(
        self,
        db: Session,
        ontology_id: str,
        *,
        display_name: str,
        source_object_type_id: str,
        target_object_type_id: str,
        name: str | None = None,
        description: str | None = None,
        cardinality: str | None = None,
        structure_type: str | None = None,
        mapping_object_type_id: str | None = None,
        operator: str | None = None,
    ) -> RelationTypeOut:
        from app.services.relation_structure import validate_relation_structure_type
        from app.services.relation_terms import compact_relation_term, validate_relation_term

        ontology = db.get(Ontology, ontology_id)
        if not ontology:
            raise ValueError("Ontology not found")

        source = db.get(ObjectType, source_object_type_id)
        if not source or source.ontology_id != ontology_id:
            raise ValueError("Invalid source object type")
        target = db.get(ObjectType, target_object_type_id)
        if not target or target.ontology_id != ontology_id:
            raise ValueError("Invalid target object type")
        if source_object_type_id == target_object_type_id:
            raise ValueError("Source and target object cannot be the same")

        term_error = validate_relation_term(display_name)
        if term_error:
            raise ValueError(term_error)
        compacted = compact_relation_term(display_name)

        if structure_type is not None:
            structure_error = validate_relation_structure_type(structure_type)
            if structure_error:
                raise ValueError(structure_error)

        if mapping_object_type_id is not None:
            mapping_obj = db.get(ObjectType, mapping_object_type_id)
            if not mapping_obj or mapping_obj.ontology_id != ontology_id:
                raise ValueError("Invalid mapping object type")
            if mapping_object_type_id in {source_object_type_id, target_object_type_id}:
                raise ValueError("Mapping object cannot be the same as source or target")

        rel_name = name or compacted
        _validate_cardinality_or_raise(cardinality)
        rel = RelationType(
            ontology_id=ontology_id,
            name=rel_name,
            display_name=compacted,
            description=description,
            source_object_type_id=source_object_type_id,
            target_object_type_id=target_object_type_id,
            cardinality=cardinality,
            structure_type=structure_type,
            mapping_object_type_id=mapping_object_type_id,
            source_confidence=0.5,
            status=EntityStatus.SUGGESTED.value,
            user_created=True,
            origin="manual",
        )
        db.add(rel)
        db.flush()
        _log_change(db, "relation_type", rel.id, "create", operator, f"新建关系：{compacted}")
        db.commit()
        return self.query._to_relation_out(db, rel)

    def convert_object_to_relation(
        self,
        db: Session,
        object_type_id: str,
        *,
        source_object_type_id: str,
        target_object_type_id: str,
        display_name: str,
        description: str | None = None,
        cardinality: str | None = None,
        structure_type: str | None = "fact_table",
        operator: str | None = None,
    ) -> tuple[RelationTypeOut, ObjectTypeSummary, list[str]]:
        """把被误判为业务对象的事实/明细/动作表转成一条业务关系。

        这类表（维修/清算/交易…）每行是一次业务**事实**而非一个实体：真正的业务对象是
        它引用的键。转换做三件事，且在一个事务内完成：

        1) 以原表为**实现表**（mapping_object）在 source/target 两端点间新建一条业务
           关系（结构类型默认 fact_table，谓词取 display_name）；原表属性因此被无损保留
           为关系的承载表，而非被丢弃。
        2) 原对象降级为 ``bridge`` 角色——离开业务对象集（数据域发布只保留 business_object，
           见 publish），不再作为假业务对象出现；role_reason 写明去向，便于审计与回滚。
        3) 端点必须都是业务对象（rule1：业务关系两端非 business_object 会在发布时被删）。
           非业务对象端点在此**自动提升**为 business_object，提升者名列入返回值供前端提示。

        可逆：删除该关系并把原对象 table_role 改回 business_object 即还原。
        改判字段（table_role/role_reason）经 ``_mark_overridden`` 钉住，不会被下次机器
        生成翻转回去。
        """
        from app.services.relation_structure import validate_relation_structure_type

        obj = db.get(ObjectType, object_type_id)
        if not obj:
            raise ValueError("Object type not found")
        ontology_id = obj.ontology_id

        if source_object_type_id == target_object_type_id:
            raise ValueError("Source and target object cannot be the same")
        if object_type_id in {source_object_type_id, target_object_type_id}:
            raise ValueError("被转换的对象不能同时作为关系端点")

        endpoints: list[ObjectType] = []
        for role_name, ep_id in (
            ("source", source_object_type_id),
            ("target", target_object_type_id),
        ):
            ep = db.get(ObjectType, ep_id)
            if not ep or ep.ontology_id != ontology_id:
                raise ValueError(f"Invalid {role_name} object type")
            endpoints.append(ep)

        term_error = validate_relation_term(display_name)
        if term_error:
            raise ValueError(term_error)
        compacted = compact_relation_term(display_name)

        if structure_type is not None:
            structure_error = validate_relation_structure_type(structure_type)
            if structure_error:
                raise ValueError(structure_error)

        # 端点必须是业务对象（rule1）：非业务对象端点自动提升，记录被提升者展示名。
        promoted: list[str] = []
        for ep in endpoints:
            if ep.table_role != "business_object":
                ep.table_role = "business_object"
                _set_review_mark(ep, False)
                # 只钉 table_role：role_reason 没被改，钉住它会冻结机器的角色依据刷新。
                _mark_overridden(ep, ["table_role"])
                _mark_edited(ep)
                _log_change(
                    db,
                    "object_type",
                    ep.id,
                    "edit",
                    operator,
                    f"提升为业务对象（作为关系「{compacted}」端点）",
                )
                promoted.append(ep.display_name)

        # 1) 建关系：原对象作为实现表(mapping_object)，无损承载其属性。
        rel = RelationType(
            ontology_id=ontology_id,
            name=obj.name or compacted,
            display_name=compacted,
            description=description,
            source_object_type_id=source_object_type_id,
            target_object_type_id=target_object_type_id,
            cardinality=cardinality,
            structure_type=structure_type,
            mapping_object_type_id=object_type_id,
            source_confidence=0.5,
            status=EntityStatus.EDITED.value,
            user_created=True,
            origin="converted",
        )
        db.add(rel)
        db.flush()
        _log_change(
            db, "relation_type", rel.id, "create", operator, f"由对象转为业务关系：{compacted}"
        )

        # 2) 退役原对象：降级 bridge、清待复核、记录去向；钉住字段防重生翻转。
        obj.table_role = "bridge"
        obj.role_reason = (
            f"已转为业务关系「{compacted}」"
            f"（{endpoints[0].display_name} → {endpoints[1].display_name}），作为该关系的实现表"
        )
        _mark_overridden(obj, ["table_role", "role_reason"])
        _mark_edited(obj)
        _log_change(
            db, "object_type", obj.id, "edit", operator, f"转为业务关系「{compacted}」"
        )

        db.commit()

        relation_out = self.query._to_relation_out(db, rel)
        object_summary = self.query._to_object_summary(db, obj)
        return relation_out, object_summary, promoted

    def update_relation_type(
        self,
        db: Session,
        relation_type_id: str,
        *,
        display_name: str | None = None,
        description: str | None = None,
        cardinality: str | None = None,
        structure_type: str | None = None,
        mapping_object_type_id: str | None = None,
        source_object_type_id: str | None = None,
        target_object_type_id: str | None = None,
        operator: str | None = None,
    ) -> RelationTypeOut:
        from app.services.relation_structure import validate_relation_structure_type

        rel = db.get(RelationType, relation_type_id)
        if not rel:
            raise ValueError("Relation type not found")

        if display_name is not None:
            term_error = validate_relation_term(display_name)
            if term_error:
                raise ValueError(term_error)
            rel.display_name = compact_relation_term(display_name)
            _mark_overridden(rel, ["display_name"])
        if description is not None:
            rel.description = description
            _mark_overridden(rel, ["description"])
        if cardinality is not None:
            _validate_cardinality_or_raise(cardinality)
            rel.cardinality = cardinality
            _mark_overridden(rel, ["cardinality"])
        if structure_type is not None:
            structure_error = validate_relation_structure_type(structure_type)
            if structure_error:
                raise ValueError(structure_error)
            rel.structure_type = structure_type
            _mark_overridden(rel, ["structure_type"])
        if mapping_object_type_id is not None:
            if mapping_object_type_id == "":
                rel.mapping_object_type_id = None
            else:
                mapping_obj = db.get(ObjectType, mapping_object_type_id)
                if not mapping_obj or mapping_obj.ontology_id != rel.ontology_id:
                    raise ValueError("Invalid mapping object type")
                if mapping_object_type_id in {
                    rel.source_object_type_id,
                    rel.target_object_type_id,
                }:
                    raise ValueError("Mapping object cannot be the same as source or target")
                rel.mapping_object_type_id = mapping_object_type_id
        if source_object_type_id is not None:
            source = db.get(ObjectType, source_object_type_id)
            if not source or source.ontology_id != rel.ontology_id:
                raise ValueError("Invalid source object type")
            if source_object_type_id == rel.target_object_type_id:
                raise ValueError("Source and target object cannot be the same")
            rel.source_object_type_id = source_object_type_id
        if target_object_type_id is not None:
            target = db.get(ObjectType, target_object_type_id)
            if not target or target.ontology_id != rel.ontology_id:
                raise ValueError("Invalid target object type")
            if target_object_type_id == rel.source_object_type_id:
                raise ValueError("Source and target object cannot be the same")
            rel.target_object_type_id = target_object_type_id

        _mark_edited(rel)

        _log_change(db, "relation_type", rel.id, "edit", operator, "更新关系")
        db.commit()
        return self.query._to_relation_out(db, rel)

    def pre_publish_relation_type(
        self,
        db: Session,
        relation_type_id: str,
        operator: str | None = None,
    ) -> RelationTypeOut:

        rel = db.get(RelationType, relation_type_id)
        if not rel:
            raise ValueError("Relation type not found")

        rel.status = EntityStatus.PRE_PUBLISHED.value
        _log_change(db, "relation_type", rel.id, "pre_publish", operator, "预发布关系")
        db.commit()
        db.refresh(rel)
        return self.query._to_relation_out(db, rel)

    def pre_publish_object_type(
        self,
        db: Session,
        object_type_id: str,
        operator: str | None = None,
    ) -> ObjectTypeSummary:

        obj = db.get(ObjectType, object_type_id)
        if not obj:
            raise ValueError("Object type not found")

        obj.status = EntityStatus.PRE_PUBLISHED.value
        _log_change(db, "object_type", obj.id, "pre_publish", operator, "预发布")
        db.commit()
        db.refresh(obj)
        return self.query._to_object_summary(db, obj)

    # --- 业务逻辑本体编辑(定义 / 预发布)---
    # 注:对象/字段引用绑定复用下方 bind_object_to_logic / bind_property_to_logic,
    # 由用户在业务逻辑详情页从已发布本体中主动挑选。

    def _resolve_published_ontology(self, db: Session, domain_id: str) -> Ontology:

        domain = db.get(DomainContext, domain_id)
        if not domain:
            raise ValueError("数据域不存在")
        ontology = self.query.get_published_ontology(db, domain_id)
        if not ontology:
            raise ValueError("该数据域尚无已发布本体,无法创建业务逻辑")
        return ontology

    def _ensure_unique_logic_name(self, db: Session, ontology_id: str, name: str) -> str:
        base = name or "business_logic"
        candidate = base
        suffix = 1
        while (
            db.query(BusinessLogic)
            .filter(
                BusinessLogic.ontology_id == ontology_id,
                BusinessLogic.name == candidate,
            )
            .first()
        ):
            suffix += 1
            candidate = f"{base}_{suffix}"
        return candidate

    def create_business_logic(
        self,
        db: Session,
        *,
        domain_id: str,
        name: str,
        display_name: str,
        logic_type: str,
        description: str | None = None,
        expression_summary: str | None = None,
        expression_draft: dict | None = None,
        expression_json: dict | None = None,
        category_id: str | None = None,
        operator: str | None = None,
    ) -> BusinessLogicDetail:

        ontology = self._resolve_published_ontology(db, domain_id)
        if logic_type not in {"metric", "tag", "rule"}:
            raise ValueError("logic_type 必须是 metric / tag / rule 之一")
        unique_name = self._ensure_unique_logic_name(db, ontology.id, name)

        if category_id is not None and not db.get(BusinessLogicCategory, category_id):
            raise ValueError("分类不存在")

        summary = expression_summary
        if summary is None and expression_draft:
            summary = self._derive_summary_from_draft(expression_draft)

        logic = BusinessLogic(
            ontology_id=ontology.id,
            category_id=category_id,
            name=unique_name,
            display_name=display_name,
            logic_type=logic_type,
            description=description,
            expression_summary=summary,
            expression_draft=(
                json.dumps(expression_draft, ensure_ascii=False) if expression_draft else None
            ),
            expression_json=(
                json.dumps(expression_json, ensure_ascii=False) if expression_json else None
            ),
            source_type="manual",
            source_ref=None,
            source_confidence=0.5,
            status=EntityStatus.SUGGESTED.value,
            user_created=True,
            origin="manual",
        )
        db.add(logic)
        db.flush()
        _log_change(db, "business_logic", logic.id, "create", operator, f"新建业务逻辑:{display_name}")
        db.commit()

        detail = self.query.get_business_logic(db, logic.id)
        if not detail:
            raise ValueError("Business logic not found")
        return detail

    def update_business_logic(
        self,
        db: Session,
        logic_id: str,
        *,
        display_name: str | None = None,
        description: str | None = None,
        logic_type: str | None = None,
        expression_summary: str | None = None,
        expression_draft: dict | None = None,
        expression_json: dict | None = None,
        category_id: str | None = None,
        operator: str | None = None,
    ) -> BusinessLogicDetail:

        logic = db.get(BusinessLogic, logic_id)
        if not logic:
            raise ValueError("Business logic not found")

        if category_id is not None:
            if category_id == "":
                logic.category_id = None
            elif not db.get(BusinessLogicCategory, category_id):
                raise ValueError("分类不存在")
            else:
                logic.category_id = category_id
        if logic_type is not None:
            if logic_type not in {"metric", "tag", "rule"}:
                raise ValueError("logic_type 必须是 metric / tag / rule 之一")
            logic.logic_type = logic_type
            _mark_overridden(logic, ["logic_type"])
        if display_name is not None:
            logic.display_name = display_name
            _mark_overridden(logic, ["display_name"])
        if description is not None:
            logic.description = description
            _mark_overridden(logic, ["description"])
        if expression_summary is not None:
            logic.expression_summary = expression_summary
            _mark_overridden(logic, ["expression_summary"])
        if expression_draft is not None:
            logic.expression_draft = json.dumps(expression_draft, ensure_ascii=False)
            if expression_summary is None:
                derived = self._derive_summary_from_draft(expression_draft)
                if derived is not None:
                    logic.expression_summary = derived
        if expression_json is not None:
            logic.expression_json = json.dumps(expression_json, ensure_ascii=False)

        _mark_edited(logic)

        _log_change(db, "business_logic", logic.id, "edit", operator, "更新业务逻辑")
        db.commit()

        detail = self.query.get_business_logic(db, logic_id)
        if not detail:
            raise ValueError("Business logic not found")
        return detail

    def create_business_logic_category(
        self,
        db: Session,
        name: str,
        description: str | None = None,
    ):
        existing = db.query(BusinessLogicCategory).filter(
            BusinessLogicCategory.name == name
        ).first()
        if existing:
            raise ValueError("分类名称已存在")
        cat = BusinessLogicCategory(name=name, description=description)
        db.add(cat)
        db.commit()
        db.refresh(cat)
        return {
            "id": cat.id,
            "name": cat.name,
            "description": cat.description,
            "logic_count": 0,
            "created_at": cat.created_at,
            "updated_at": cat.updated_at,
        }

    def update_business_logic_category(
        self,
        db: Session,
        category_id: str,
        name: str | None = None,
        description: str | None = None,
    ):
        cat = db.get(BusinessLogicCategory, category_id)
        if not cat:
            raise ValueError("分类不存在")
        if name is not None:
            existing = db.query(BusinessLogicCategory).filter(
                BusinessLogicCategory.name == name,
                BusinessLogicCategory.id != category_id,
            ).first()
            if existing:
                raise ValueError("分类名称已存在")
            cat.name = name
        if description is not None:
            cat.description = description
        db.commit()
        db.refresh(cat)
        count = db.query(BusinessLogic).filter(
            BusinessLogic.category_id == cat.id
        ).count()
        return {
            "id": cat.id,
            "name": cat.name,
            "description": cat.description,
            "logic_count": count,
            "created_at": cat.created_at,
            "updated_at": cat.updated_at,
        }

    def delete_business_logic_category(
        self,
        db: Session,
        category_id: str,
    ):
        cat = db.get(BusinessLogicCategory, category_id)
        if not cat:
            raise ValueError("分类不存在")
        db.delete(cat)
        db.commit()
        return {"id": category_id, "deleted": True}

    @staticmethod
    def _derive_summary_from_draft(expression_draft: dict) -> str | None:
        try:
            from app.services.expression_formatter import (
                _parse_draft,
                _segments_to_summary,
            )

            segments, refs = _parse_draft(expression_draft)
            if not segments:
                return None
            return _segments_to_summary(segments, refs)
        except Exception:
            return None

    def pre_publish_business_logic(
        self,
        db: Session,
        logic_id: str,
        operator: str | None = None,
    ) -> BusinessLogicOut:
        logic = db.get(BusinessLogic, logic_id)
        if not logic:
            raise ValueError("Business logic not found")

        logic.status = EntityStatus.PRE_PUBLISHED.value
        _log_change(db, "business_logic", logic.id, "pre_publish", operator, "预发布业务逻辑")
        db.commit()
        db.refresh(logic)


        return self.query._to_business_logic_out(db, logic)

    # --- 业务逻辑绑定（对象 / 字段）---

    def _logic_or_raise(self, db: Session, logic_id: str) -> BusinessLogic:
        logic = db.get(BusinessLogic, logic_id)
        if not logic:
            raise ValueError("Business logic not found")
        return logic

    def _object_same_ontology(
        self, db: Session, logic: BusinessLogic, object_type_id: str
    ) -> ObjectType:
        obj = db.get(ObjectType, object_type_id)
        if not obj or obj.ontology_id != logic.ontology_id:
            raise ValueError("Object type not found or not in the same ontology")
        return obj

    def _property_same_ontology(
        self, db: Session, logic: BusinessLogic, property_id: str
    ) -> Property:
        prop = db.get(Property, property_id)
        if not prop:
            raise ValueError("Property not found")
        obj = db.get(ObjectType, prop.object_type_id)
        if not obj or obj.ontology_id != logic.ontology_id:
            raise ValueError("Property not found or not in the same ontology")
        return prop

    def bind_object_to_logic(
        self,
        db: Session,
        logic_id: str,
        object_type_id: str,
        *,
        role: str = "subject",
        operator: str | None = None,
    ) -> BusinessLogicObjectBindingOut:
        if role not in _OBJECT_BINDING_ROLES:
            raise ValueError(f"Invalid role, allowed: {sorted(_OBJECT_BINDING_ROLES)}")
        logic = self._logic_or_raise(db, logic_id)
        obj = self._object_same_ontology(db, logic, object_type_id)

        existing = (
            db.query(BusinessLogicObjectBinding)
            .filter(
                BusinessLogicObjectBinding.business_logic_id == logic.id,
                BusinessLogicObjectBinding.object_type_id == obj.id,
                BusinessLogicObjectBinding.role == role,
            )
            .first()
        )
        if existing:
            if existing.source == "inferred":
                existing.source = "manual"
                db.commit()
                db.refresh(existing)
            return BusinessLogicObjectBindingOut.model_validate(
                self._enrich_object_binding(db, existing)
            )

        binding = BusinessLogicObjectBinding(
            business_logic_id=logic.id,
            object_type_id=obj.id,
            role=role,
            source="manual",
        )
        db.add(binding)
        db.flush()
        _log_change(
            db,
            "business_logic",
            logic.id,
            "bind_object",
            operator,
            f"绑定对象 {obj.display_name}（role={role}）",
        )
        db.commit()
        db.refresh(binding)
        return BusinessLogicObjectBindingOut.model_validate(
            self._enrich_object_binding(db, binding)
        )

    def unbind_object_from_logic(
        self,
        db: Session,
        binding_id: str,
        *,
        operator: str | None = None,
    ) -> dict:
        binding = db.get(BusinessLogicObjectBinding, binding_id)
        if not binding:
            raise ValueError("Object binding not found")
        logic_id = binding.business_logic_id
        object_type_id = binding.object_type_id
        role = binding.role
        db.delete(binding)
        _log_change(
            db,
            "business_logic",
            logic_id,
            "unbind_object",
            operator,
            f"解绑对象 {object_type_id}（role={role}）",
        )
        db.commit()
        return {"id": binding_id, "deleted": True}

    def bind_property_to_logic(
        self,
        db: Session,
        logic_id: str,
        property_id: str,
        *,
        role: str = "input",
        operator: str | None = None,
    ) -> BusinessLogicPropertyBindingOut:
        if role not in _PROPERTY_BINDING_ROLES:
            raise ValueError(f"Invalid role, allowed: {sorted(_PROPERTY_BINDING_ROLES)}")
        logic = self._logic_or_raise(db, logic_id)
        prop = self._property_same_ontology(db, logic, property_id)

        existing = (
            db.query(BusinessLogicPropertyBinding)
            .filter(
                BusinessLogicPropertyBinding.business_logic_id == logic.id,
                BusinessLogicPropertyBinding.property_id == prop.id,
                BusinessLogicPropertyBinding.role == role,
            )
            .first()
        )
        if existing:
            if existing.source == "inferred":
                existing.source = "manual"
                db.commit()
                db.refresh(existing)
            return BusinessLogicPropertyBindingOut.model_validate(
                self._enrich_property_binding(db, existing)
            )

        binding = BusinessLogicPropertyBinding(
            business_logic_id=logic.id,
            property_id=prop.id,
            role=role,
            source="manual",
        )
        db.add(binding)
        db.flush()
        _log_change(
            db,
            "business_logic",
            logic.id,
            "bind_property",
            operator,
            f"绑定字段 {prop.display_name}（role={role}）",
        )
        db.commit()
        db.refresh(binding)
        return BusinessLogicPropertyBindingOut.model_validate(
            self._enrich_property_binding(db, binding)
        )

    def unbind_property_from_logic(
        self,
        db: Session,
        binding_id: str,
        *,
        operator: str | None = None,
    ) -> dict:
        binding = db.get(BusinessLogicPropertyBinding, binding_id)
        if not binding:
            raise ValueError("Property binding not found")
        logic_id = binding.business_logic_id
        property_id = binding.property_id
        role = binding.role
        db.delete(binding)
        _log_change(
            db,
            "business_logic",
            logic_id,
            "unbind_property",
            operator,
            f"解绑字段 {property_id}（role={role}）",
        )
        db.commit()
        return {"id": binding_id, "deleted": True}

    @staticmethod
    def _enrich_object_binding(
        db: Session, binding: BusinessLogicObjectBinding
    ) -> BusinessLogicObjectBinding:
        obj = db.get(ObjectType, binding.object_type_id)
        # 通过 setattr 附加显示字段；Pydantic from_attributes 会读取这些属性
        if obj:
            setattr(binding, "object_type_name", obj.name)
            setattr(binding, "object_type_display_name", obj.display_name)
        return binding

    @staticmethod
    def _enrich_property_binding(
        db: Session, binding: BusinessLogicPropertyBinding
    ) -> BusinessLogicPropertyBinding:
        prop = db.get(Property, binding.property_id)
        if prop:
            setattr(binding, "property_name", prop.name)
            setattr(binding, "property_display_name", prop.display_name)
            obj = db.get(ObjectType, prop.object_type_id)
            if obj:
                setattr(binding, "object_type_id", obj.id)
                setattr(binding, "object_type_name", obj.name)
        return binding
