from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import edit_service, query
from app.database import get_db
from app.schemas import (
    ObjectTypeDetail,
    ObjectTypeSummary,
    ObjectTypeUpdate,
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
    limit: int | None = Query(None, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    return query.list_object_types(
        db, ontology_id=ontology_id, q=q, limit=limit, offset=offset
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

