from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import edit_service, provenance_service, query
from app.database import get_db
from app.models import ObjectType, Property
from app.schemas import (
    ClusterDetail,
    ConflictResolveRequest,
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
    ValidationIssueOut,
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
        limit=limit,
        offset=offset,
    )


class OntologyPropertyOption(BaseModel):
    """本体字段选项：供结构化 Spec 表单的字段下拉使用（metric 的 properties/group_by/filters）。"""

    name: str
    display_name: str
    object_type_name: str


@router.get(
    "/ontologies/{ontology_id}/properties",
    response_model=list[OntologyPropertyOption],
)
def list_ontology_properties(ontology_id: str, db: Session = Depends(get_db)):
    """列出某本体下全部字段（跨对象），供手动结构化 Spec 表单下拉。

    与 validation._check_ontology_refs 的 known_properties 查询同构：spec 里引用的字段
    名必须在此集合内，否则校验闸门报 unknown_property。
    """
    rows = (
        db.query(Property.name, Property.display_name, ObjectType.name)
        .join(ObjectType, Property.object_type_id == ObjectType.id)
        .filter(ObjectType.ontology_id == ontology_id)
        .order_by(ObjectType.name, Property.name)
        .all()
    )
    return [
        OntologyPropertyOption(
            name=name, display_name=display_name, object_type_name=obj_name
        )
        for (name, display_name, obj_name) in rows
    ]


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
def get_ontology_grouped_graph(ontology_id: str, db: Session = Depends(get_db)):
    return query.get_ontology_grouped_graph(db, ontology_id)


@router.get(
    "/ontologies/{ontology_id}/clusters/{cluster_id}",
    response_model=ClusterDetail,
)
def get_ontology_cluster_detail(
    ontology_id: str, cluster_id: str, db: Session = Depends(get_db)
):
    """单个聚类下钻：全量成员 + 簇内关系边，供前端邻接矩阵视图。

    cluster_id 取自同一本体的 grouped-graph 返回（默认聚类粒度）。
    """
    detail = query.get_ontology_cluster_detail(db, ontology_id, cluster_id)
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
    limit: int | None = Query(None, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    return query.list_relation_types(
        db, ontology_id=ontology_id, q=q, display_name=display_name, limit=limit, offset=offset
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
    limit: int | None = Query(None, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    return query.list_relation_types(
        db,
        ontology_id=ontology_id,
        domain_context_id=domain_id,
        published_only=published_only,
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
    db: Session = Depends(get_db),
):
    """按 display_name 去重的关系分组列表（关系 Tab 用）。"""
    return query.list_relation_groups(
        db,
        ontology_id=ontology_id,
        domain_context_id=domain_id,
        published_only=published_only,
        q=q,
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
            operator=data.operator,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/relation-types/{relation_type_id}/pre-publish", response_model=RelationTypeOut)
def pre_publish_relation_type(
    relation_type_id: str,
    db: Session = Depends(get_db),
):
    try:
        return edit_service.pre_publish_relation_type(db, relation_type_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

