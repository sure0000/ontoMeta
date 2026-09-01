import json

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import edit_service, provenance_service, publish_service, query
from app.database import get_db
from app.models import ObjectType, Property
from app.services import dataset_catalog, derived_object, unclaimed_tables
from app.schemas import (
    ClaimTableRequest,
    ClusterDetail,
    ConflictResolveRequest,
    DatasetOut,
    DerivedDefinitionOut,
    DerivedObjectCreate,
    DerivedObjectCreated,
    UnclaimedTableOut,
    UnclaimedTablesOut,
    FieldPinRequest,
    FormalIssueOut,
    FormalValidationResult,
    ObjectToRelationConvertIn,
    ObjectToRelationConvertResult,
    ObjectTypeBatchUpdate,
    ObjectTypeBatchUpdateResult,
    ObjectTypeDetail,
    ObjectTypeSummary,
    ObjectTypeUpdate,
    OntologyConflictsOut,
    OntologyGraph,
    OntologyGroupedGraph,
    OntologySummary,
    OntologyValidationResult,
    PageResult,
    PropertyOut,
    PropertyUpdate,
    RelationTypeCreate,
    RelationTypeDetail,
    RelationGroupOut,
    RelationTypeOut,
    RelationTypeUpdate,
    RelationTypeBatchUpdate,
    RelationTypeBatchUpdateResult,
    ReviewModeStats,
    ReviewQueueOut,
    SegmentDetail,
    SegmentSummary,
    SegmentUpdate,
    ValidationIssueOut,
    VerbRefinementBatchApplyRequest,
    VerbRefinementBatchOut,
    VerbSuggestion,
    VersionDiffOut,
    VersionRecordOut,
    VersionSnapshotOut,
)

router = APIRouter()

_ALLOWED_ROLE_IN = {"business_object", "data_table", "bridge", "technical"}


def _parse_role_in(raw: str | None) -> list[str] | None:
    """把逗号分隔的角色列表解析为合法取值列表；空/全非法返回 None。"""
    if not raw:
        return None
    values = [v.strip() for v in raw.split(",") if v.strip() in _ALLOWED_ROLE_IN]
    return values or None


@router.get(
    "/ontologies/{ontology_id}/conflicts", response_model=OntologyConflictsOut
)
def list_ontology_conflicts(ontology_id: str, db: Session = Depends(get_db)):
    """列出本体下所有字段级待复核冲突（再生成后人工与上游双改）。"""
    return provenance_service.list_conflicts(db, ontology_id)


@router.post("/conflicts/resolve")
def resolve_conflict(data: ConflictResolveRequest, db: Session = Depends(get_db)):
    try:
        return provenance_service.resolve_conflict(
            db,
            data.entity_type,
            data.entity_id,
            data.field,
            data.resolution,
            operator=data.operator,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/ontologies/{ontology_id}/conflicts/resolve-all")
def resolve_all_conflicts(
    ontology_id: str,
    resolution: str = Query(..., description="accept_theirs | keep_ours"),
    db: Session = Depends(get_db),
):
    """一键解决本体下全部字段冲突。"""
    if resolution not in {"accept_theirs", "keep_ours"}:
        raise HTTPException(status_code=400, detail="resolution 必须是 accept_theirs / keep_ours")
    return provenance_service.resolve_all_conflicts(db, ontology_id, resolution)


@router.post("/fields/pin")
def set_field_pin(data: FieldPinRequest, db: Session = Depends(get_db)):
    """钉住/放开某个字段：钉住后再生成不会覆盖；放开后交回机器接管。"""
    try:
        return provenance_service.set_pin(
            db,
            data.entity_type,
            data.entity_id,
            data.field,
            data.pinned,
            operator=data.operator,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

@router.get("/ontologies", response_model=list[OntologySummary])
def list_ontologies(
    domain_id: str | None = Query(None),
    published_only: bool = Query(False),
    db: Session = Depends(get_db),
):
    return query.list_ontologies(db, domain_context_id=domain_id, published_only=published_only)


@router.get("/ontologies/{ontology_id}", response_model=OntologySummary)
def get_ontology(ontology_id: str, db: Session = Depends(get_db)):
    ontology = query.get_ontology(db, ontology_id)
    if not ontology:
        raise HTTPException(status_code=404, detail="Ontology not found")
    return ontology


@router.get("/ontologies/{ontology_id}/object-types", response_model=PageResult[ObjectTypeSummary])
def list_object_types_by_ontology(
    ontology_id: str,
    q: str | None = Query(None),
    role_in: str | None = Query(
        None, description="对象角色多选(逗号分隔)：business_object,data_table,bridge,technical"
    ),
    needs_review: bool | None = Query(None, description="仅看待复核=true"),
    segment_id: str | None = Query(None),
    limit: int | None = Query(None, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    return query.list_object_types(
        db,
        ontology_id=ontology_id,
        q=q,
        role_in=_parse_role_in(role_in),
        needs_review=needs_review,
        segment_id=segment_id,
        limit=limit,
        offset=offset,
    )


class OntologyPropertyOption(BaseModel):
    """本体字段选项：供结构化 Spec 表单的字段下拉使用（metric 的 properties/group_by/filters）。"""

    name: str
    display_name: str
    object_type_name: str
    data_type: str | None = None
    semantic_type: str | None = None
    #: 是否命中身份命名约定（``<对象>_id`` / ``id``）。同步表单据此给主键的默认值——
    #: 只在**有把握**时给，猜的不给（见 ontology_projection.primary_key_is_confident）。
    is_identity: bool = False


@router.get(
    "/ontologies/{ontology_id}/properties",
    response_model=list[OntologyPropertyOption],
)
def list_ontology_properties(
    ontology_id: str,
    object_type: str | None = None,
    db: Session = Depends(get_db),
):
    """列出某本体下的字段，供结构化 Spec 表单下拉。

    与 validation._check_ontology_refs 的 known_properties 查询同构：spec 里引用的字段
    名必须在此集合内，否则校验闸门报 unknown_property。

    ``object_type``：只列这一个对象的字段。同步任务的主键/增量字段/sequence 列都必须
    是**所选那张表**上的列——跨对象混列会让人从 700 张表的字段里挑一个根本不在目标表上
    的列，选完才在执行期炸。缺省仍返回全本体（metric 的多对象口径要跨对象选）。
    """
    q = (
        db.query(
            Property.name,
            Property.display_name,
            ObjectType.name,
            Property.data_type,
            Property.semantic_type,
        )
        .join(ObjectType, Property.object_type_id == ObjectType.id)
        .filter(ObjectType.ontology_id == ontology_id)
    )
    if object_type:
        q = q.filter(ObjectType.name == object_type)
    rows = q.order_by(ObjectType.name, Property.name).all()
    identity = _identity_columns(rows)
    return [
        OntologyPropertyOption(
            name=name,
            display_name=display_name,
            object_type_name=obj_name,
            data_type=data_type,
            semantic_type=semantic_type,
            is_identity=(obj_name, name) in identity,
        )
        for (name, display_name, obj_name, data_type, semantic_type) in rows
    ]


def _identity_columns(rows: list) -> set[tuple[str, str]]:
    """按命名约定判定的身份列 ``(对象名, 字段名)``。判据复用本体投影那一份，不另立。"""
    from app.services.ontology_projection import primary_key_is_confident, primary_key_name

    by_object: dict[str, list[tuple[str, str | None]]] = {}
    for name, _display, obj_name, _dt, semantic_type in rows:
        by_object.setdefault(obj_name, []).append((name, semantic_type))
    out: set[tuple[str, str]] = set()
    for obj_name, props in by_object.items():
        names = [n for n, _ in props]
        identifiers = [n for n, st in props if (st or "") == "identifier"]
        if primary_key_is_confident(obj_name, names, identifiers):
            pk = primary_key_name(obj_name, names, identifiers)
            if pk:
                out.add((obj_name, pk))
    return out


@router.get("/ontologies/{ontology_id}/graph", response_model=OntologyGraph)
def get_ontology_graph(
    ontology_id: str,
    center_id: str | None = Query(None, description="邻域展开中心对象 ID"),
    depth: int = Query(1, ge=0, le=5),
    full: bool = Query(False, description="为 true 时返回全量图（大域慎用）"),
    max_nodes: int = Query(80, ge=10, le=500),
    published_only: bool = Query(False),
    db: Session = Depends(get_db),
):
    return query.get_ontology_graph(
        db,
        ontology_id,
        center_id=center_id,
        depth=depth,
        full=full,
        max_nodes=max_nodes,
        published_only=published_only,
    )


@router.get("/ontologies/{ontology_id}/grouped-graph", response_model=OntologyGroupedGraph)
def get_ontology_grouped_graph(
    ontology_id: str,
    published_only: bool = Query(False),
    db: Session = Depends(get_db)
):
    """获取本体的板块地图视图。

    Args:
        published_only: True = 只读已发布状态，False = 读草稿态（默认）
    """
    return query.get_ontology_grouped_graph(db, ontology_id, published_only=published_only)


@router.get(
    "/ontologies/{ontology_id}/clusters/{cluster_id}",
    response_model=ClusterDetail,
)
def get_ontology_cluster_detail(
    ontology_id: str,
    cluster_id: str,
    published_only: bool = Query(False),
    db: Session = Depends(get_db),
):
    """单个聚类下钻：全量成员 + 簇内关系边，供前端邻接矩阵视图。

    cluster_id 取自同一本体的 grouped-graph 返回（默认聚类粒度）。
    """
    detail = query.get_ontology_cluster_detail(
        db, ontology_id, cluster_id, published_only=published_only
    )
    if detail is None:
        raise HTTPException(status_code=404, detail="聚类不存在或已随数据变化失效，请刷新概览图")
    return detail


@router.get("/ontologies/{ontology_id}/versions", response_model=list[VersionRecordOut])
def list_ontology_versions(ontology_id: str, db: Session = Depends(get_db)):
    return query.list_versions(db, ontology_id)


@router.get(
    "/ontologies/{ontology_id}/versions/{version}/diff",
    response_model=VersionDiffOut,
)
def get_ontology_version_diff(
    ontology_id: str, version: int, db: Session = Depends(get_db)
):
    diff = query.get_version_diff(db, ontology_id, version)
    if not diff:
        raise HTTPException(status_code=404, detail="Version not found")
    return diff


@router.get(
    "/ontologies/{ontology_id}/versions/{version}/snapshot",
    response_model=VersionSnapshotOut,
)
def get_ontology_version_snapshot(
    ontology_id: str, version: int, db: Session = Depends(get_db)
):
    snapshot = query.get_version_snapshot(db, ontology_id, version)
    if not snapshot:
        raise HTTPException(status_code=404, detail="Version not found")
    return snapshot


@router.post(
    "/ontologies/{ontology_id}/validate",
    response_model=OntologyValidationResult,
)
def validate_ontology(ontology_id: str, db: Session = Depends(get_db)):
    from app.services.draft_consistency import validate_ontology as run_validate

    ontology = query.get_ontology(db, ontology_id)
    if not ontology:
        raise HTTPException(status_code=404, detail="Ontology not found")
    issues = run_validate(db, ontology_id)
    return OntologyValidationResult(
        ontology_id=ontology_id,
        ok=len(issues) == 0,
        issues=[ValidationIssueOut(**i.to_dict()) for i in issues],
    )


@router.get("/ontologies/{ontology_id}/publish-preflight")
def publish_preflight(ontology_id: str, db: Session = Depends(get_db)):
    """发布前自检：将发布多少对象/属性/关系、将跳过多少、为什么。

    与 publish() 共用 select_publishable，两边不会漂移。
    """
    try:
        return publish_service.preflight(db, ontology_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/ontologies/{ontology_id}/formal-validate",
    response_model=FormalValidationResult,
)
def formal_validate_ontology(ontology_id: str, db: Session = Depends(get_db)):
    """形式化不变式预检（F2）：派生无环 / 口径可解析 / 聚合语义自洽 / 基数良定义。

    与 ``/validate``（引用完整性）互补。ok=无 error 级违反；warning 不影响 ok
    但会列出。``enforcement=error`` 时，ok=False 将在发布时被阻断。
    """
    from app.config import settings as env_settings
    from app.services.ontology_formal import check_formal_invariants

    ontology = query.get_ontology(db, ontology_id)
    if not ontology:
        raise HTTPException(status_code=404, detail="Ontology not found")
    issues = check_formal_invariants(db, ontology_id)
    errors = [i for i in issues if i.severity == "error"]
    warnings = [i for i in issues if i.severity == "warning"]
    return FormalValidationResult(
        ontology_id=ontology_id,
        ok=len(errors) == 0,
        enforcement=getattr(env_settings, "formal_enforcement", "warn"),
        error_count=len(errors),
        warning_count=len(warnings),
        issues=[FormalIssueOut(**i.to_dict()) for i in issues],
    )


@router.get("/ontologies/{ontology_id}/datasets", response_model=list[DatasetOut])
def list_ontology_datasets(
    ontology_id: str,
    layer: str | None = Query(None, description="ods / dim / dwd / dws / ads"),
    q: str | None = Query(None, description="按实体名或物理表名过滤"),
    source_ready_only: bool = Query(False, description="只列可作下游作业源表的"),
    queryable_only: bool = Query(False, description="只列可直接查询的"),
    db: Session = Depends(get_db),
):
    """本体在数仓里的数据集目录：每个已登记的落点一条，带稳定引用 ``ref``。

    这是「同步完之后指得到那张表」的入口。它是**读模型**，由接入契约与 Projection 聚合
    而成（见 ``services/dataset_catalog``），不是新的权威源，也不落任何表。
    """
    if not query.get_ontology(db, ontology_id):
        raise HTTPException(status_code=404, detail="Ontology not found")
    entries = dataset_catalog.list_datasets(
        db,
        ontology_id,
        layer=layer,
        q=q,
        source_ready_only=source_ready_only,
        queryable_only=queryable_only,
    )
    return [DatasetOut.model_validate(entry) for entry in entries]


@router.post(
    "/ontologies/{ontology_id}/derived-objects",
    response_model=DerivedObjectCreated,
)
def create_derived_object(
    ontology_id: str,
    body: DerivedObjectCreate,
    db: Session = Depends(get_db),
):
    """由数仓里的若干数据集派生一个**新粒度**的业务对象。

    这是「本体同步到数仓之后」唯一该新增实体的场景：多表 join 出的宽表/汇总表，一行
    代表的东西变了，它是新的业务概念。1:1 的搬运与清洗**不走这里**——那只是同一个对象
    的另一个落点（见 ``services/derived_object`` 与 ``services/object_landing``）。

    新对象仍落在同一个本体里：一域一本体，不会因为加工出一张新表就多出一个本体。
    """
    if not query.get_ontology(db, ontology_id):
        raise HTTPException(status_code=404, detail="Ontology not found")
    payload = derived_object.DerivedObjectInput(
        name=body.name,
        display_name=body.display_name,
        grain=body.grain,
        upstream_refs=list(body.upstream_refs),
        fields=[
            derived_object.FieldSource(
                property=f.property,
                from_ref=f.from_ref,
                from_column=f.from_column,
                display_name=f.display_name,
            )
            for f in body.fields
        ],
        description=body.description,
        joins=[
            derived_object.UpstreamJoin(
                left_ref=j.left_ref,
                right_ref=j.right_ref,
                how=j.how,
                on=[
                    derived_object.JoinCondition(left=c.left, right=c.right) for c in j.on
                ],
            )
            for j in body.joins
        ],
        layer=body.layer,
        notes=body.notes,
    )
    try:
        result = derived_object.create_derived_object(db, ontology_id, payload)
    except derived_object.DerivedObjectError as exc:
        # 定义不成立是用户输入问题（缺粒度/缺连接条件/上游不在目录里），不是服务器故障。
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return DerivedObjectCreated(**vars(result))


@router.get(
    "/object-types/{object_type_id}/derived-definition",
    response_model=DerivedDefinitionOut,
)
def get_derived_definition(object_type_id: str, db: Session = Depends(get_db)):
    """派生定义：上游、粒度、连接条件、字段来源。非派生对象 404。"""
    view = derived_object.get_definition(db, object_type_id)
    if view is None:
        raise HTTPException(status_code=404, detail="该对象不是派生对象")
    return DerivedDefinitionOut(
        object_type_id=view.object_type_id,
        grain=view.grain,
        layer=view.layer,
        upstreams=[DatasetOut.model_validate(e) for e in view.upstreams],
        dangling_refs=list(view.dangling_refs),
        joins=list(view.joins),
        field_mapping=list(view.field_mapping),
        notes=view.notes,
    )


@router.get(
    "/ontologies/{ontology_id}/unclaimed-tables", response_model=UnclaimedTablesOut
)
def list_unclaimed_tables(
    ontology_id: str,
    datasource_id: str | None = Query(None),
    database: str | None = Query(None, description="只扫这个库；默认扫本体自己写过的库"),
    db: Session = Depends(get_db),
):
    """数仓里存在、本体里没人认领的表。**实时扫库**（慢接口）。

    只给两个出路：认领为已有实体的落点，或者不管它。**不提供「照着表建对象」**——
    照物理表反推出来的对象正是重复对象的来源。
    """
    if not query.get_ontology(db, ontology_id):
        raise HTTPException(status_code=404, detail="Ontology not found")
    try:
        items, scanned = unclaimed_tables.list_unclaimed_tables(
            db, ontology_id, datasource_id=datasource_id, database=database
        )
    except unclaimed_tables.UnclaimedTableError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 —— 连不上数仓是环境问题，不是请求错误
        raise HTTPException(status_code=502, detail=f"读取数仓表列表失败：{exc}") from exc
    return UnclaimedTablesOut(
        items=[
            UnclaimedTableOut(
                database=item.database,
                table=item.table,
                physical=item.physical,
                layer=item.layer,
            )
            for item in items
        ],
        scanned_databases=scanned,
    )


@router.post("/ontologies/{ontology_id}/claim-table", response_model=DatasetOut)
def claim_table(
    ontology_id: str, body: ClaimTableRequest, db: Session = Depends(get_db)
):
    """把一张无主表登记为某个已有对象的落点（不新建对象）。

    认领只登记归属：不代表平台搬过这张表的数据，故不写最近成功时间、也不放行查询网关。
    """
    if not query.get_ontology(db, ontology_id):
        raise HTTPException(status_code=404, detail="Ontology not found")
    try:
        entry = unclaimed_tables.claim_table(
            db,
            ontology_id,
            object_type_id=body.object_type_id,
            database=body.database,
            table=body.table,
            datasource_id=body.datasource_id,
        )
    except unclaimed_tables.UnclaimedTableError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return DatasetOut.model_validate(entry)


@router.get("/object-types", response_model=PageResult[ObjectTypeSummary])
def list_object_types(
    ontology_id: str | None = Query(None),
    domain_id: str | None = Query(None),
    published_only: bool = Query(False),
    q: str | None = Query(None),
    role_in: str | None = Query(
        None, description="对象角色多选(逗号分隔)：business_object,data_table,bridge,technical"
    ),
    needs_review: bool | None = Query(None, description="仅看待复核=true"),
    segment_id: str | None = Query(None),
    limit: int | None = Query(None, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    return query.list_object_types(
        db,
        ontology_id=ontology_id,
        domain_context_id=domain_id,
        published_only=published_only,
        q=q,
        role_in=_parse_role_in(role_in),
        needs_review=needs_review,
        segment_id=segment_id,
        limit=limit,
        offset=offset,
    )


@router.patch("/object-types/batch", response_model=ObjectTypeBatchUpdateResult)
def batch_update_object_types(
    data: ObjectTypeBatchUpdate,
    db: Session = Depends(get_db),
):
    """批量修改对象类型的角色与复核状态（数据域页面多选批量操作）。

    注意：本路由须声明在 ``/object-types/{object_type_id}`` 之前，
    否则 "batch" 会被当作 object_type_id 捕获。
    """
    try:
        items = edit_service.batch_update_object_types(
            db,
            data.ids,
            table_role=data.table_role,
            needs_review=data.needs_review,
            operator=data.operator,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ObjectTypeBatchUpdateResult(updated=len(items), items=items)


@router.get("/object-types/{object_type_id}", response_model=ObjectTypeDetail)
def get_object_type(
    object_type_id: str,
    published_only: bool = Query(False),
    db: Session = Depends(get_db),
):
    obj = query.get_object_type(db, object_type_id, published_only=published_only)
    if not obj:
        raise HTTPException(status_code=404, detail="Object type not found")
    return obj


@router.patch("/object-types/{object_type_id}", response_model=ObjectTypeDetail)
def update_object_type(
    object_type_id: str,
    data: ObjectTypeUpdate,
    db: Session = Depends(get_db),
):
    try:
        return edit_service.update_object_type(
            db,
            object_type_id,
            name=data.name,
            display_name=data.display_name,
            description=data.description,
            table_role=data.table_role,
            segment_id=data.segment_id,
            needs_review=data.needs_review,
            operator=data.operator,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post(
    "/object-types/{object_type_id}/convert-to-relation",
    response_model=ObjectToRelationConvertResult,
)
def convert_object_to_relation(
    object_type_id: str,
    data: ObjectToRelationConvertIn,
    db: Session = Depends(get_db),
):
    """把被误判为业务对象的事实/明细/动作表转成一条业务关系（原表作为实现表）。"""
    try:
        relation, retired_object, promoted = edit_service.convert_object_to_relation(
            db,
            object_type_id,
            source_object_type_id=data.source_object_type_id,
            target_object_type_id=data.target_object_type_id,
            display_name=data.display_name,
            description=data.description,
            cardinality=data.cardinality,
            structure_type=data.structure_type,
            operator=data.operator,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ObjectToRelationConvertResult(
        relation=relation,
        retired_object=retired_object,
        promoted_endpoints=promoted,
    )


@router.patch("/object-types/{object_type_id}/pre-publish", response_model=ObjectTypeSummary)
def pre_publish_object_type(
    object_type_id: str,
    db: Session = Depends(get_db),
):
    try:
        return edit_service.pre_publish_object_type(db, object_type_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/properties/{property_id}", response_model=PropertyOut)
def update_property(
    property_id: str,
    data: PropertyUpdate,
    db: Session = Depends(get_db),
):
    try:
        return edit_service.update_property(
            db,
            property_id,
            display_name=data.display_name,
            description=data.description,
            data_type=data.data_type,
            semantic_type=data.semantic_type,
            operator=data.operator,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/ontologies/{ontology_id}/relation-types", response_model=PageResult[RelationTypeOut])
def list_relation_types_by_ontology(
    ontology_id: str,
    q: str | None = Query(None),
    display_name: str | None = Query(None),
    published_only: bool = Query(False),
    needs_review: bool | None = Query(None),
    limit: int | None = Query(None, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    return query.list_relation_types(
        db,
        ontology_id=ontology_id,
        published_only=published_only,
        needs_review=needs_review,
        q=q,
        display_name=display_name,
        limit=limit,
        offset=offset,
    )


@router.post("/relation-types", response_model=RelationTypeOut)
def create_relation_type(data: RelationTypeCreate, db: Session = Depends(get_db)):
    try:
        return edit_service.create_relation_type(
            db,
            data.ontology_id,
            display_name=data.display_name,
            source_object_type_id=data.source_object_type_id,
            target_object_type_id=data.target_object_type_id,
            name=data.name,
            description=data.description,
            cardinality=data.cardinality,
            structure_type=data.structure_type,
            mapping_object_type_id=data.mapping_object_type_id,
            operator=data.operator,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/relation-types", response_model=PageResult[RelationTypeOut])
def list_relation_types(
    ontology_id: str | None = Query(None),
    domain_id: str | None = Query(None),
    published_only: bool = Query(False),
    q: str | None = Query(None),
    display_name: str | None = Query(None),
    needs_review: bool | None = Query(None),
    limit: int | None = Query(None, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    return query.list_relation_types(
        db,
        ontology_id=ontology_id,
        domain_context_id=domain_id,
        published_only=published_only,
        needs_review=needs_review,
        q=q,
        display_name=display_name,
        limit=limit,
        offset=offset,
    )


@router.get("/relation-groups", response_model=list[RelationGroupOut])
def list_relation_groups(
    ontology_id: str | None = Query(None),
    domain_id: str | None = Query(None),
    published_only: bool = Query(False),
    q: str | None = Query(None),
    needs_review: bool | None = Query(None),
    db: Session = Depends(get_db),
):
    """按 display_name 去重的关系分组列表（关系 Tab 用）。"""
    return query.list_relation_groups(
        db,
        ontology_id=ontology_id,
        domain_context_id=domain_id,
        published_only=published_only,
        q=q,
        needs_review=needs_review,
    )


@router.get("/relation-types/{relation_type_id}", response_model=RelationTypeDetail)
def get_relation_type(
    relation_type_id: str,
    published_only: bool = Query(False),
    db: Session = Depends(get_db),
):
    rel = query.get_relation_type(db, relation_type_id, published_only=published_only)
    if not rel:
        raise HTTPException(status_code=404, detail="Relation type not found")
    return rel


@router.patch("/relation-types/batch", response_model=RelationTypeBatchUpdateResult)
def batch_update_relation_types(
    data: RelationTypeBatchUpdate,
    db: Session = Depends(get_db),
):
    """批量置关系复核状态（审核工作台的关系队列）。

    与 ``/object-types/batch`` 对称；同样必须声明在 ``/{relation_type_id}`` 之前，
    否则 "batch" 会被当成 id 捕获。
    """
    try:
        items = edit_service.batch_update_relation_types(
            db, data.ids, needs_review=data.needs_review, operator=data.operator
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RelationTypeBatchUpdateResult(updated=len(items), items=items)


@router.patch("/relation-types/{relation_type_id}", response_model=RelationTypeOut)
def update_relation_type(
    relation_type_id: str,
    data: RelationTypeUpdate,
    db: Session = Depends(get_db),
):
    try:
        return edit_service.update_relation_type(
            db,
            relation_type_id,
            display_name=data.display_name,
            description=data.description,
            cardinality=data.cardinality,
            structure_type=data.structure_type,
            mapping_object_type_id=data.mapping_object_type_id,
            source_object_type_id=data.source_object_type_id,
            target_object_type_id=data.target_object_type_id,
            needs_review=data.needs_review,
            operator=data.operator,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/relation-types/{relation_type_id}")
def delete_relation_type(
    relation_type_id: str,
    operator: str | None = Query(None),
    db: Session = Depends(get_db),
):
    try:
        return edit_service.delete_relation_type(db, relation_type_id, operator=operator)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.patch("/relation-types/{relation_type_id}/pre-publish", response_model=RelationTypeOut)
def pre_publish_relation_type(
    relation_type_id: str,
    db: Session = Depends(get_db),
):
    try:
        return edit_service.pre_publish_relation_type(db, relation_type_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ------------------------------------------------------------------
# 板块 (Segments)
# ------------------------------------------------------------------


@router.get("/ontologies/{ontology_id}/segments", response_model=PageResult[SegmentSummary])
def list_segments(
    ontology_id: str,
    published_only: bool = Query(False),
    q: str | None = Query(None),
    limit: int | None = Query(None, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """列出本体的业务板块。"""
    return query.list_segments(
        db,
        ontology_id=ontology_id,
        published_only=published_only,
        q=q,
        limit=limit,
        offset=offset,
    )


@router.get("/segments/{segment_id}", response_model=SegmentDetail)
def get_segment(
    segment_id: str,
    published_only: bool = Query(False),
    db: Session = Depends(get_db),
):
    """获取板块详情（包含成员列表）。"""
    segment = query.get_segment_detail(db, segment_id, published_only=published_only)
    if not segment:
        raise HTTPException(status_code=404, detail="Segment not found")
    return segment


@router.patch("/segments/{segment_id}", response_model=SegmentDetail)
def update_segment(
    segment_id: str,
    data: SegmentUpdate,
    db: Session = Depends(get_db),
):
    try:
        result = edit_service.update_segment(
            db,
            segment_id,
            name=data.name,
            display_name=data.display_name,
            description=data.description,
            operator=data.operator,
        )
        if result is None:
            raise ValueError("Segment not found")
        return result
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/ontologies/{ontology_id}/review-stats", response_model=ReviewModeStats)
def get_review_stats(
    ontology_id: str,
    db: Session = Depends(get_db),
):
    """获取审核模式的全局统计和板块级进度。"""
    return query.get_review_mode_stats(db, ontology_id)


@router.get("/ontologies/{ontology_id}/review-queue", response_model=ReviewQueueOut)
def get_review_queue(
    ontology_id: str,
    kind: str = Query("object", pattern="^(object|relation)$", description="队列类型"),
    segment_id: str | None = Query(
        None, description='只看某板块；"-" 表示未接入板块的对象'
    ),
    role_in: list[str] | None = Query(None, description="只看这些角色（默认全部角色）"),
    limit: int = Query(20, ge=1, le=100, description="返回多少**组**"),
    cursor: str | None = Query(None, description="上一页返回的 next_cursor（组 key）"),
    db: Session = Depends(get_db),
):
    """审核队列：把待复核对象按「板块 × 角色 × 命名族 × 判定强度」聚成组。

    为什么不复用 ``GET /object-types?needs_review=true``：那条路按 ``updated_at DESC``
    排序，而判定动作既改写 ``updated_at`` 又把行移出结果集，翻页会静默跳过整页。
    本接口的排序键不含任何随判定变化的字段，游标可重放。
    """
    return query.get_review_queue(
        db,
        ontology_id,
        kind=kind,
        segment_id=segment_id,
        role_in=role_in,
        limit=limit,
        cursor=cursor,
    )


@router.post("/ontologies/{ontology_id}/verb-refinement/suggest")
async def suggest_verb_refinements(
    ontology_id: str,
    db: Session = Depends(get_db),
):
    """
    生成空动词细化建议（S2）。

    扫描本体中空泛动词的关系（"属于"、"引用"、"关联"），
    根据外键列名规则推断精确动词，返回建议列表。
    """
    from app.models import RelationType
    from app.services.verb_refiner import suggest_verb_refinements as generate_suggestions
    from app.schemas import VerbRefinementBatchOut, VerbSuggestion

    # 查询需要细化的关系：空泛动词
    empty_verbs = {"属于", "引用", "关联", "关系", "连接"}
    relations = (
        db.query(RelationType)
        .filter(
            RelationType.ontology_id == ontology_id,
            RelationType.deleted_by_user == False,
        )
        .all()
    )

    # 过滤出空泛动词的关系
    candidates = [
        rel for rel in relations
        if not rel.display_name or rel.display_name in empty_verbs
    ]

    # 生成规则建议；规则未覆盖的关系随后一次性送入 LLM。
    raw_suggestions = generate_suggestions(candidates)

    fallback_relations = [
        rel for rel, suggestion in zip(candidates, raw_suggestions)
        if suggestion.get("method") == "fallback"
    ]
    if fallback_relations:
        from app.services.settings_service import SettingsService

        runtime = SettingsService().get_llm_runtime(db)
        if runtime.api_key and runtime.model:
            from openai import AsyncOpenAI
            from app.services.common import make_async_http_client
            from app.services.verb_refiner import build_llm_renaming_prompt

            client = AsyncOpenAI(
                api_key=runtime.api_key,
                base_url=runtime.api_base_url or None,
                timeout=30,
                max_retries=1,
                http_client=make_async_http_client(),
            )
            try:
                completion = await client.chat.completions.create(
                    model=runtime.model,
                    messages=[
                        {"role": "system", "content": "你是数据治理专家，只输出合法 JSON 数组。"},
                        {"role": "user", "content": build_llm_renaming_prompt(fallback_relations)},
                    ],
                    temperature=0.1,
                )
                response_text = (completion.choices[0].message.content or "{}").strip()
                if response_text.startswith("```"):
                    lines = response_text.splitlines()
                    response_text = "\n".join(lines[1:-1]).strip()
                payload = json.loads(response_text)
                llm_items = payload if isinstance(payload, list) else payload.get("items", [])
                by_index = {
                    int(item["index"]): str(item["verb"]).strip()
                    for item in llm_items
                    if isinstance(item, dict) and str(item.get("verb", "")).strip()
                }
                fallback_ids = {rel.id: i for i, rel in enumerate(fallback_relations)}
                for suggestion in raw_suggestions:
                    index = fallback_ids.get(suggestion["relation_id"])
                    verb = by_index.get(index) if index is not None else None
                    if verb:
                        suggestion["suggested_verb"] = verb
                        suggestion["method"] = "llm"
                        suggestion["confidence"] = 0.7
            except Exception:
                # Suggestions remain actionable through the deterministic fallback.
                pass
            finally:
                await client.close()

    # 转换为响应格式
    suggestions = []
    for sug in raw_suggestions:
        rel = next((r for r in candidates if r.id == sug["relation_id"]), None)
        if not rel:
            continue

        suggestions.append(VerbSuggestion(
            relation_id=sug["relation_id"],
            current_verb=sug["current_verb"],
            suggested_verb=sug["suggested_verb"],
            method=sug["method"],
            confidence=sug["confidence"],
            source_object_name=rel.source_object_type.display_name or rel.source_object_type.name,
            target_object_name=rel.target_object_type.display_name or rel.target_object_type.name,
        ))

    # 统计
    rule_count = sum(1 for s in suggestions if s.method == "rule")
    llm_count = sum(1 for s in suggestions if s.method == "llm")
    fallback_count = sum(1 for s in suggestions if s.method == "fallback")

    return VerbRefinementBatchOut(
        suggestions=suggestions,
        total=len(suggestions),
        rule_count=rule_count,
        llm_count=llm_count,
        fallback_count=fallback_count,
    )


@router.post("/ontologies/{ontology_id}/verb-refinement/apply")
def apply_verb_refinements(
    ontology_id: str,
    request: VerbRefinementBatchApplyRequest,
    db: Session = Depends(get_db),
):
    """
    批量应用动词细化建议（S2）。

    **采纳即已复核**：调用方是人（在审核台勾选了这些建议），采纳就是一次判定动作，
    落 ``needs_review=False``。此前这里置 True——于是「细化动词」每跑一次就把 1288 条
    关系重新打成待复核，净增审核债，而人明明刚刚看过它们。
    """
    from app.models import RelationType
    from app.services.edit import _mark_edited, _mark_overridden
    from app.services.common import log_change
    from app.services.relation_terms import compact_relation_term, validate_relation_term

    updated_count = 0
    errors = []

    for item in request.items:
        rel = db.get(RelationType, item.relation_id)
        if not rel:
            errors.append(f"关系 {item.relation_id} 不存在")
            continue

        if rel.ontology_id != ontology_id:
            errors.append(f"关系 {item.relation_id} 不属于本体 {ontology_id}")
            continue

        term_error = validate_relation_term(item.new_verb)
        if term_error:
            errors.append(f"关系 {item.relation_id}：{term_error}")
            continue

        # 更新动词并钉住人工值，避免下一轮机器生成覆盖采纳结果。
        old_verb = rel.display_name
        rel.display_name = compact_relation_term(item.new_verb)
        _mark_overridden(rel, ["display_name"])
        # 人采纳了这条建议 = 这条关系的动词已经过人工确认。
        rel.needs_review = False
        _mark_edited(rel)
        log_change(
            db,
            "relation_type",
            rel.id,
            "update",
            item.operator or request.operator or "system",
            f"动词细化: {old_verb} -> {rel.display_name}",
        )

        updated_count += 1

    db.commit()

    return {
        "updated_count": updated_count,
        "total_requested": len(request.items),
        "errors": errors,
    }
