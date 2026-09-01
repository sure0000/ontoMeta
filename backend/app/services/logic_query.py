from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.models import (
    BusinessLogic,
    BusinessLogicCategory,
    BusinessLogicObjectBinding,
    BusinessLogicPropertyBinding,
    EntityStatus,
    ObjectType,
    Property,
    RelationType,
)
from app.schemas import (
    BusinessLogicDetail,
    BusinessLogicObjectBindingOut,
    BusinessLogicOut,
    BusinessLogicPropertyBindingOut,
    BusinessLogicPropertyOption,
    BusinessLogicRef,
    LogicLandingOut,
    ObjectTypeDetail,
    ObjectTypeSummary,
    PageResult,
    PropertyOut,
)
from app.services.object_landing import (
    LogicLanding,
    bulk_logic_landings,
    bulk_object_landings,
)
from app.services.ontology_query import (
    OntologyQueryService as _OntologyQueryBase,
    _loads_json,
    _logic_referenced_ids,
    _logic_relates_to_object,
    _logic_text_blob,
)


class OntologyQueryService(_OntologyQueryBase):
    """只读查询服务（业务逻辑扩展）。"""

    def _to_business_logic_out(
        self,
        db: Session,
        logic: BusinessLogic,
        *,
        domain: tuple[str | None, str | None] | None = None,
        binding_counts: tuple[int, int] | None = None,
        category_name: str | None = None,
        landing: LogicLanding | None = None,
        landing_loaded: bool = False,
    ) -> BusinessLogicOut:
        if domain is None:
            domain_id, domain_name = self._resolve_domain_context(db, logic.ontology_id)
        else:
            domain_id, domain_name = domain
        if binding_counts is None:
            binding_counts = self._bulk_business_logic_binding_counts(db, [logic.id]).get(
                logic.id, (0, 0)
            )
        bound_object_count, bound_property_count = binding_counts
        # 与对象落点同构的自愈：批量调用传入，单条调用就地补查。
        if landing is None and not landing_loaded:
            landing = bulk_logic_landings(db, [logic.id]).get(logic.id)
        if category_name is None and logic.category_id:
            cat = db.get(BusinessLogicCategory, logic.category_id)
            if cat:
                category_name = cat.name
        return BusinessLogicOut(
            id=logic.id,
            name=logic.name,
            display_name=logic.display_name,
            logic_type=logic.logic_type,
            description=logic.description,
            expression_summary=logic.expression_summary,
            expression_draft=_loads_json(logic.expression_draft),
            expression_json=_loads_json(logic.expression_json),
            source_type=logic.source_type,
            source_ref=logic.source_ref,
            status=logic.status,
            source_confidence=logic.source_confidence,
            domain_context_id=domain_id,
            domain_name=domain_name,
            category_id=logic.category_id,
            category_name=category_name,
            bound_object_count=bound_object_count,
            bound_property_count=bound_property_count,
            landing=(
                LogicLandingOut.model_validate(landing) if landing is not None else None
            ),
            updated_at=logic.updated_at,
        )

    def _bulk_business_logic_binding_counts(
        self, db: Session, logic_ids: list[str]
    ) -> dict[str, tuple[int, int]]:
        """批量计算每个 business_logic 的 (bound_object_count, bound_property_count)。

        计数 = 显式绑定 ∪ 表达式引用（expression_json/expression_draft 中的 refs）
        去重后的对象/字段数。
        """
        if not logic_ids:
            return {}
        logics = (
            db.query(BusinessLogic)
            .filter(BusinessLogic.id.in_(logic_ids))
            .all()
        )
        obj_binding_rows = (
            db.query(
                BusinessLogicObjectBinding.business_logic_id,
                BusinessLogicObjectBinding.object_type_id,
            )
            .filter(BusinessLogicObjectBinding.business_logic_id.in_(logic_ids))
            .distinct()
            .all()
        )
        prop_binding_rows = (
            db.query(
                BusinessLogicPropertyBinding.business_logic_id,
                BusinessLogicPropertyBinding.property_id,
            )
            .filter(
                BusinessLogicPropertyBinding.business_logic_id.in_(logic_ids),
                BusinessLogicPropertyBinding.property_id.isnot(None),
            )
            .distinct()
            .all()
        )
        obj_bound: dict[str, set[str]] = {lid: set() for lid in logic_ids}
        prop_bound: dict[str, set[str]] = {lid: set() for lid in logic_ids}
        for lid, oid in obj_binding_rows:
            obj_bound[lid].add(oid)
        for lid, pid in prop_binding_rows:
            prop_bound[lid].add(pid)
        ref_obj: dict[str, set[str]] = {lid: set() for lid in logic_ids}
        ref_prop: dict[str, set[str]] = {lid: set() for lid in logic_ids}
        for logic in logics:
            oids, pids = _logic_referenced_ids(logic)
            ref_obj[logic.id] |= oids
            ref_prop[logic.id] |= pids
        return {
            lid: (
                len(obj_bound[lid] | ref_obj[lid]),
                len(prop_bound[lid] | ref_prop[lid]),
            )
            for lid in logic_ids
        }

    def _logic_object_binding_ids(
        self, db: Session, logic: BusinessLogic
    ) -> list[str]:
        rows = (
            db.query(BusinessLogicObjectBinding.object_type_id)
            .filter(BusinessLogicObjectBinding.business_logic_id == logic.id)
            .distinct()
            .all()
        )
        return [r[0] for r in rows]

    def _object_logic_ids(self, db: Session, obj: ObjectType) -> list[str]:
        rows = (
            db.query(BusinessLogicObjectBinding.business_logic_id)
            .filter(BusinessLogicObjectBinding.object_type_id == obj.id)
            .distinct()
            .all()
        )
        return [r[0] for r in rows]

    def _related_logics_for_object(
        self, db: Session, obj: ObjectType
    ) -> list[BusinessLogic]:
        """返回与该对象关联的业务逻辑。

        关联判定 = 显式绑定（BusinessLogicObjectBinding）
                ∪ 表达式引用（expression_json/expression_draft 中 refs 引用了该对象）。
        """
        bound_ids = set(self._object_logic_ids(db, obj))
        referenced_ids = self._object_referenced_logic_map(db, [obj.id]).get(obj.id, set())
        all_ids = bound_ids | referenced_ids
        if not all_ids:
            return []
        return (
            db.query(BusinessLogic)
            .filter(BusinessLogic.id.in_(all_ids))
            .order_by(BusinessLogic.updated_at.desc())
            .all()
        )

    def _object_referenced_logic_map(
        self, db: Session, object_ids: list[str]
    ) -> dict[str, set[str]]:
        """返回 object_type_id -> {business_logic_id} 基于表达式引用。

        仅扫描传入对象所属本体下的业务逻辑，避免全表扫描。
        """
        if not object_ids:
            return {}
        rows = (
            db.query(ObjectType.id, ObjectType.ontology_id)
            .filter(ObjectType.id.in_(object_ids))
            .all()
        )
        obj_to_ontology = {oid: oid_ for oid, oid_ in rows}
        if not obj_to_ontology:
            return {oid: set() for oid in object_ids}
        ontology_ids = set(obj_to_ontology.values())
        logics = (
            db.query(BusinessLogic)
            .filter(BusinessLogic.ontology_id.in_(list(ontology_ids)))
            .all()
        )
        result: dict[str, set[str]] = {oid: set() for oid in object_ids}
        for logic in logics:
            ref_obj_ids, _ = _logic_referenced_ids(logic)
            for oid in ref_obj_ids:
                if oid in result:
                    result[oid].add(logic.id)
        return result

    def _related_objects_for_logic(
        self, db: Session, logic: BusinessLogic
    ) -> list[ObjectType]:
        """返回与该业务逻辑关联的对象。

        关联判定 = 显式绑定 ∪ 表达式引用（refs 中提到的对象）。
        """
        bound_ids = set(self._logic_object_binding_ids(db, logic))
        ref_obj_ids, _ = _logic_referenced_ids(logic)
        all_ids = bound_ids | ref_obj_ids
        if not all_ids:
            return []
        return (
            db.query(ObjectType)
            .filter(ObjectType.id.in_(all_ids))
            .order_by(ObjectType.display_name.asc())
            .all()
        )

    def _logic_bindings_for_object(
        self, db: Session, obj: ObjectType
    ) -> list:
        from app.schemas import ObjectTypeLogicBindingOut

        rows = (
            db.query(BusinessLogicObjectBinding, BusinessLogic)
            .join(
                BusinessLogic,
                BusinessLogic.id == BusinessLogicObjectBinding.business_logic_id,
            )
            .filter(BusinessLogicObjectBinding.object_type_id == obj.id)
            .order_by(BusinessLogicObjectBinding.created_at.desc())
            .all()
        )
        return [
            ObjectTypeLogicBindingOut(
                binding_id=b.id,
                role=b.role,
                source=b.source,
                confidence=b.confidence,
                logic_id=logic.id,
                logic_name=logic.name,
                logic_display_name=logic.display_name,
                logic_type=logic.logic_type,
                logic_status=logic.status,
                created_at=b.created_at,
            )
            for b, logic in rows
            if logic is not None
        ]

    def _related_properties_for_logic(
        self, db: Session, logic: BusinessLogic
    ) -> list[Property]:
        """返回与该业务逻辑关联的字段。

        关联判定 = 显式绑定（BusinessLogicPropertyBinding）∪ 表达式引用。
        """
        bound_ids = {
            r[0]
            for r in (
                db.query(BusinessLogicPropertyBinding.property_id)
                .filter(BusinessLogicPropertyBinding.business_logic_id == logic.id)
                .distinct()
                .all()
            )
        }
        _, ref_prop_ids = _logic_referenced_ids(logic)
        all_ids = bound_ids | ref_prop_ids
        if not all_ids:
            return []
        return db.query(Property).filter(Property.id.in_(all_ids)).all()

    def _object_bindings_for_logic(
        self, db: Session, logic: BusinessLogic
    ) -> list[BusinessLogicObjectBindingOut]:
        rows = (
            db.query(BusinessLogicObjectBinding, ObjectType)
            .outerjoin(
                ObjectType,
                ObjectType.id == BusinessLogicObjectBinding.object_type_id,
            )
            .filter(BusinessLogicObjectBinding.business_logic_id == logic.id)
            .order_by(BusinessLogicObjectBinding.created_at.desc())
            .all()
        )
        return [
            BusinessLogicObjectBindingOut(
                id=b.id,
                business_logic_id=b.business_logic_id,
                object_type_id=b.object_type_id,
                object_type_name=obj.name if obj else None,
                object_type_display_name=obj.display_name if obj else None,
                role=b.role,
                source=b.source,
                confidence=b.confidence,
                created_at=b.created_at,
            )
            for b, obj in rows
        ]

    def _property_bindings_for_logic(
        self, db: Session, logic: BusinessLogic
    ) -> list[BusinessLogicPropertyBindingOut]:
        rows = (
            db.query(BusinessLogicPropertyBinding, Property, ObjectType)
            .outerjoin(
                Property, Property.id == BusinessLogicPropertyBinding.property_id
            )
            .outerjoin(ObjectType, ObjectType.id == Property.object_type_id)
            .filter(BusinessLogicPropertyBinding.business_logic_id == logic.id)
            .order_by(BusinessLogicPropertyBinding.created_at.desc())
            .all()
        )
        return [
            BusinessLogicPropertyBindingOut(
                id=b.id,
                business_logic_id=b.business_logic_id,
                property_id=b.property_id,
                property_name=prop.name if prop else None,
                property_display_name=prop.display_name if prop else None,
                object_type_id=obj.id if obj else None,
                object_type_name=obj.name if obj else None,
                role=b.role,
                source=b.source,
                confidence=b.confidence,
                created_at=b.created_at,
            )
            for b, prop, obj in rows
        ]

    def get_object_type(
        self, db: Session, object_type_id: str, *, published_only: bool = False
    ) -> ObjectTypeDetail | None:
        obj = (
            db.query(ObjectType)
            .options(joinedload(ObjectType.properties))
            .filter(ObjectType.id == object_type_id)
            .first()
        )
        if not obj:
            return None
        # 已发布浏览：对象本身未发布则视为不存在，且其关系/邻居仅取已发布，
        # 与 Data Agent 的接地集（只认 published 实体）保持一致，避免
        # 「浏览可见但 Data Agent 拒答」的口径分裂。
        if published_only and obj.status != EntityStatus.PUBLISHED.value:
            return None
        _pub = EntityStatus.PUBLISHED.value

        def _rel_query(filter_expr):
            q = (
                db.query(RelationType)
                .options(
                    joinedload(RelationType.source_object_type),
                    joinedload(RelationType.target_object_type),
                    joinedload(RelationType.mapping_object_type),
                )
                .filter(filter_expr)
            )
            if published_only:
                q = q.filter(RelationType.status == _pub)
            return q.all()

        outgoing = _rel_query(RelationType.source_object_type_id == object_type_id)
        incoming = _rel_query(RelationType.target_object_type_id == object_type_id)
        # 本对象作为关系表(bridge)实现(mapping)的业务关系：它本身不是关系端点，
        # 故不在 outgoing/incoming 里；供桥表详情图谱显示它所连接的两个业务对象。
        implemented = _rel_query(RelationType.mapping_object_type_id == object_type_id)
        related_logics = self._related_logics_for_object(db, obj)
        logic_bindings = self._logic_bindings_for_object(db, obj)

        # 详情与列表都基于显式绑定计数，二者保持一致。
        base_stats = self._bulk_object_stats(db, [obj.id]).get(obj.id, (0, 0, 0))
        full_stats = (base_stats[0], base_stats[1], len(related_logics))
        summary = self._to_object_summary(db, obj, stats=full_stats)
        related_logic_ids = [logic.id for logic in related_logics]
        related_domain_map = self._bulk_resolve_domain_context(
            db, [logic.ontology_id for logic in related_logics]
        )
        related_binding_map = self._bulk_business_logic_binding_counts(
            db, related_logic_ids
        )
        related_logic_landings = bulk_logic_landings(db, related_logic_ids)
        source_ref, datahub_url = self._resolve_object_datahub(db, obj)
        versions = self.list_versions(db, obj.id)
        ontology_versions = self.list_versions(db, obj.ontology_id)
        return ObjectTypeDetail(
            # 详情用 _resolve_object_datahub 解析后的 source_ref（可能回溯到父对象），
            # 与摘要里那份原始值不同——排掉摘要的，以免同名 kwarg 撞车。
            **summary.model_dump(exclude={"source_ref"}),
            ontology_id=obj.ontology_id,
            source_ref=source_ref,
            datahub_url=datahub_url,
            properties=[PropertyOut.model_validate(p) for p in obj.properties],
            outgoing_relations=[self._to_relation_out(db, r) for r in outgoing],
            incoming_relations=[self._to_relation_out(db, r) for r in incoming],
            implemented_relations=[self._to_relation_out(db, r) for r in implemented],
            business_logics=[
                self._to_business_logic_out(
                    db,
                    logic,
                    domain=related_domain_map.get(logic.ontology_id, (None, None)),
                    binding_counts=related_binding_map.get(logic.id, (0, 0)),
                    landing=related_logic_landings.get(logic.id),
                    landing_loaded=True,
                )
                for logic in related_logics
            ],
            business_logic_bindings=logic_bindings,
            version_records=versions + ontology_versions,
        )

    def list_business_logics(
        self,
        db: Session,
        ontology_id: str | None = None,
        domain_context_id: str | None = None,
        category_id: str | None = None,
        uncategorized: bool = False,
        published_only: bool = False,
        *,
        q: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> PageResult[BusinessLogicOut]:
        query = db.query(BusinessLogic)
        query = self._apply_ontology_scope(
            db,
            query,
            ontology_id=ontology_id,
            domain_context_id=domain_context_id,
            published_only=published_only,
            ontology_model=BusinessLogic,
        )
        if uncategorized:
            query = query.filter(BusinessLogic.category_id.is_(None))
        elif category_id is not None:
            query = query.filter(BusinessLogic.category_id == category_id)
        if q and q.strip():
            like = f"%{q.strip()}%"
            query = query.filter(
                (BusinessLogic.name.ilike(like))
                | (BusinessLogic.display_name.ilike(like))
                | (BusinessLogic.description.ilike(like))
            )
        total = query.count()
        query = query.order_by(BusinessLogic.updated_at.desc())
        if offset:
            query = query.offset(max(0, offset))
        if limit is not None:
            query = query.limit(max(0, limit))
        items = query.all()
        if not items:
            return PageResult(items=[], total=total, limit=limit, offset=offset)
        logic_ids = [it.id for it in items]
        domain_map = self._bulk_resolve_domain_context(db, [it.ontology_id for it in items])
        binding_map = self._bulk_business_logic_binding_counts(db, logic_ids)
        logic_landings = bulk_logic_landings(db, logic_ids)
        category_ids = {it.category_id for it in items if it.category_id}
        category_names: dict[str, str] = {}
        if category_ids:
            cats = (
                db.query(BusinessLogicCategory.id, BusinessLogicCategory.name)
                .filter(BusinessLogicCategory.id.in_(list(category_ids)))
                .all()
            )
            category_names = {cid: name for cid, name in cats}
        return PageResult(
            items=[
                self._to_business_logic_out(
                    db,
                    it,
                    domain=domain_map.get(it.ontology_id, (None, None)),
                    binding_counts=binding_map.get(it.id, (0, 0)),
                    category_name=category_names.get(it.category_id) if it.category_id else None,
                    landing=logic_landings.get(it.id),
                    landing_loaded=True,
                )
                for it in items
            ],
            total=total,
            limit=limit,
            offset=offset,
        )

    def list_business_logic_categories(self, db: Session):
        cats = (
            db.query(BusinessLogicCategory)
            .order_by(BusinessLogicCategory.updated_at.desc())
            .all()
        )
        if not cats:
            return []
        count_rows = (
            db.query(BusinessLogic.category_id, func.count(BusinessLogic.id))
            .filter(BusinessLogic.category_id.in_([c.id for c in cats]))
            .group_by(BusinessLogic.category_id)
            .all()
        )
        count_map = {cid: n for cid, n in count_rows}
        return [
            {
                "id": cat.id,
                "name": cat.name,
                "description": cat.description,
                "logic_count": count_map.get(cat.id, 0),
                "created_at": cat.created_at,
                "updated_at": cat.updated_at,
            }
            for cat in cats
        ]


    def get_business_logic(self, db: Session, logic_id: str) -> BusinessLogicDetail | None:
        logic = db.get(BusinessLogic, logic_id)
        if not logic:
            return None

        related_objects = self._related_objects_for_logic(db, logic)
        related_properties = self._related_properties_for_logic(db, logic)
        object_bindings = self._object_bindings_for_logic(db, logic)
        property_bindings = self._property_bindings_for_logic(db, logic)
        versions = self.list_versions(db, logic.id)
        ontology_versions = self.list_versions(db, logic.ontology_id)

        available_object_types, available_properties = self._available_targets_for_logic(db, logic)

        related_object_logics = self._batch_object_logic_refs(db, [o.id for o in related_objects], exclude_logic_id=logic.id)
        related_landings = bulk_object_landings(db, [o.id for o in related_objects])

        return BusinessLogicDetail(
            **self._to_business_logic_out(db, logic).model_dump(),
            related_object_types=[
                self._to_object_summary(
                    db, obj, landing=related_landings.get(obj.id), landing_loaded=True
                )
                for obj in related_objects
            ],
            related_object_logics=related_object_logics,
            related_properties=[PropertyOut.model_validate(p) for p in related_properties],
            object_bindings=object_bindings,
            property_bindings=property_bindings,
            version_records=versions + ontology_versions,
            ontology_id=logic.ontology_id,
            available_object_types=available_object_types,
            available_properties=available_properties,
        )

    def _available_targets_for_logic(
        self, db: Session, logic: BusinessLogic
    ) -> tuple[list[ObjectTypeSummary], list[BusinessLogicPropertyOption]]:
        """从业务逻辑所属本体(应为已发布本体)中取出可被引用的对象与字段候选。"""
        objects = (
            db.query(ObjectType)
            .filter(ObjectType.ontology_id == logic.ontology_id)
            .order_by(ObjectType.name)
            .all()
        )
        # 候选是整个本体的对象（ERP 域近千个）：落点必须批量取，逐个自愈补查会打出上千条 SQL。
        landings = bulk_object_landings(db, [obj.id for obj in objects])
        object_summaries = [
            self._to_object_summary(
                db, obj, landing=landings.get(obj.id), landing_loaded=True
            )
            for obj in objects
        ]
        if not objects:
            return [], []

        obj_by_id = {obj.id: obj for obj in objects}
        properties = (
            db.query(Property)
            .filter(Property.object_type_id.in_(list(obj_by_id.keys())))
            .order_by(Property.object_type_id, Property.name)
            .all()
        )
        property_options = [
            BusinessLogicPropertyOption(
                property_id=prop.id,
                property_name=prop.name,
                property_display_name=prop.display_name,
                object_type_id=prop.object_type_id,
                object_type_name=obj_by_id[prop.object_type_id].name,
                object_type_display_name=obj_by_id[prop.object_type_id].display_name,
            )
            for prop in properties
        ]
        return object_summaries, property_options

    def _batch_object_logic_refs(
        self,
        db: Session,
        object_type_ids: list[str],
        exclude_logic_id: str | None = None,
    ) -> dict[str, list[BusinessLogicRef]]:
        """批量获取多个对象上绑定的业务逻辑引用，按 object_type_id 分组。"""
        if not object_type_ids:
            return {}
        rows = (
            db.query(BusinessLogicObjectBinding, BusinessLogic)
            .join(BusinessLogic, BusinessLogic.id == BusinessLogicObjectBinding.business_logic_id)
            .filter(BusinessLogicObjectBinding.object_type_id.in_(object_type_ids))
            .all()
        )
        result: dict[str, list[BusinessLogicRef]] = {oid: [] for oid in object_type_ids}
        for binding, logic in rows:
            if exclude_logic_id and logic.id == exclude_logic_id:
                continue
            ref = BusinessLogicRef(
                id=logic.id,
                name=logic.name,
                display_name=logic.display_name,
                logic_type=logic.logic_type,
                status=logic.status,
            )
            if ref not in result[binding.object_type_id]:
                result[binding.object_type_id].append(ref)
        return result
