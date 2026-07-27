from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import edit_service, provenance_service, query
from app.database import get_db
from app.schemas import (
    ConflictResolveRequest,
    FieldPinRequest,
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


@router.get("/ontologies/{ontology_id}/graph", response_model=OntologyGraph)
def get_ontology_graph(
    ontology_id: str,
    center_id: str | None = Query(None, description="邻域展开中心对象 ID"),
    depth: int = Query(1, ge=0, le=5),
    full: bool = Query(False, description="为 true 时返回全量图（大域慎用）"),
    max_nodes: int = Query(80, ge=10, le=500),
    db: Session = Depends(get_db),
):
    return query.get_ontology_graph(
        db,
        ontology_id,
        center_id=center_id,
        depth=depth,
        full=full,
        max_nodes=max_nodes,
    )


@router.get("/ontologies/{ontology_id}/grouped-graph", response_model=OntologyGroupedGraph)
def get_ontology_grouped_graph(ontology_id: str, db: Session = Depends(get_db)):
    return query.get_ontology_grouped_graph(db, ontology_id)


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


@router.get("/object-types/{object_type_id}", response_model=ObjectTypeDetail)
def get_object_type(object_type_id: str, db: Session = Depends(get_db)):
    obj = query.get_object_type(db, object_type_id)
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
    limit: int | None = Query(None, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    return query.list_relation_types(
        db, ontology_id=ontology_id, q=q, limit=limit, offset=offset
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
        limit=limit,
        offset=offset,
    )


@router.get("/relation-types/{relation_type_id}", response_model=RelationTypeDetail)
def get_relation_type(relation_type_id: str, db: Session = Depends(get_db)):
    rel = query.get_relation_type(db, relation_type_id)
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

