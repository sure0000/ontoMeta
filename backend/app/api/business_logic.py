from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import (
    confirmation_service,
    edit_service,
    expression_formatter_service,
    logic_import_service,
    query,
)
from app.database import get_db
from app.models import BusinessLogic, ObjectType
from app.schemas import (
    BusinessLogicCategoryCreate,
    BusinessLogicCategoryOut,
    BusinessLogicCategoryUpdate,
    BusinessLogicCreate,
    BusinessLogicDetail,
    BusinessLogicImportRequest,
    BusinessLogicObjectBindingCreate,
    BusinessLogicObjectBindingOut,
    BusinessLogicOut,
    BusinessLogicPropertyBindingCreate,
    BusinessLogicPropertyBindingOut,
    BusinessLogicUpdate,
    ConfirmationOut,
    ExpressionFormatRequest,
    ExpressionFormatResponse,
    PageResult,
)

router = APIRouter()

@router.get("/business-logics", response_model=PageResult[BusinessLogicOut])
def list_business_logics(
    ontology_id: str | None = Query(None),
    domain_id: str | None = Query(None),
    category_id: str | None = Query(None),
    published_only: bool = Query(False),
    q: str | None = Query(None),
    limit: int | None = Query(None, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    return query.list_business_logics(
        db,
        ontology_id=ontology_id,
        domain_context_id=domain_id,
        category_id=category_id,
        published_only=published_only,
        q=q,
        limit=limit,
        offset=offset,
    )


@router.get("/business-logic-categories", response_model=list[BusinessLogicCategoryOut])
def list_business_logic_categories(db: Session = Depends(get_db)):
    return query.list_business_logic_categories(db)


@router.post("/business-logic-categories", response_model=BusinessLogicCategoryOut)
def create_business_logic_category(data: BusinessLogicCategoryCreate, db: Session = Depends(get_db)):
    try:
        return edit_service.create_business_logic_category(db, data.name, data.description)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/business-logic-categories/{category_id}", response_model=BusinessLogicCategoryOut)
def update_business_logic_category(
    category_id: str, data: BusinessLogicCategoryUpdate, db: Session = Depends(get_db)
):
    try:
        return edit_service.update_business_logic_category(
            db, category_id, name=data.name, description=data.description
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/business-logic-categories/{category_id}")
def delete_business_logic_category(category_id: str, db: Session = Depends(get_db)):
    try:
        return edit_service.delete_business_logic_category(db, category_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/business-logics/{logic_id}", response_model=BusinessLogicDetail)
def get_business_logic(logic_id: str, db: Session = Depends(get_db)):
    logic = query.get_business_logic(db, logic_id)
    if not logic:
        raise HTTPException(status_code=404, detail="Business logic not found")
    return logic


@router.post(
    "/business-logics/{logic_id}/object-bindings",
    response_model=BusinessLogicObjectBindingOut,
)
def create_object_binding(
    logic_id: str,
    data: BusinessLogicObjectBindingCreate,
    db: Session = Depends(get_db),
):
    if data.business_logic_id != logic_id:
        raise HTTPException(status_code=400, detail="business_logic_id mismatch")
    try:
        return edit_service.bind_object_to_logic(
            db,
            logic_id,
            data.object_type_id,
            role=data.role,
            operator=data.operator,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/business-logics/object-bindings/{binding_id}")
def delete_object_binding(binding_id: str, db: Session = Depends(get_db)):
    try:
        return edit_service.unbind_object_from_logic(db, binding_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/business-logics/{logic_id}/property-bindings",
    response_model=BusinessLogicPropertyBindingOut,
)
def create_property_binding(
    logic_id: str,
    data: BusinessLogicPropertyBindingCreate,
    db: Session = Depends(get_db),
):
    if data.business_logic_id != logic_id:
        raise HTTPException(status_code=400, detail="business_logic_id mismatch")
    try:
        return edit_service.bind_property_to_logic(
            db,
            logic_id,
            data.property_id,
            role=data.role,
            operator=data.operator,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/business-logics/property-bindings/{binding_id}")
def delete_property_binding(binding_id: str, db: Session = Depends(get_db)):
    try:
        return edit_service.unbind_property_from_logic(db, binding_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/business-logics", response_model=BusinessLogicDetail)
def create_business_logic(data: BusinessLogicCreate, db: Session = Depends(get_db)):
    try:
        return edit_service.create_business_logic(
            db,
            domain_id=data.domain_id,
            name=data.name,
            display_name=data.display_name,
            logic_type=data.logic_type,
            description=data.description,
            expression_summary=data.expression_summary,
            expression_draft=data.expression_draft,
            expression_json=data.expression_json,
            category_id=data.category_id,
            operator=data.operator,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post(
    "/business-logics/format-expression",
    response_model=ExpressionFormatResponse,
)
async def format_expression(data: ExpressionFormatRequest, db: Session = Depends(get_db)):
    try:
        return await expression_formatter_service.format(
            db,
            domain_id=data.domain_id,
            expression_draft=data.expression_draft,
            logic_type=data.logic_type,
            description=data.description,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/business-logics/import", response_model=BusinessLogicDetail)
async def import_business_logic(data: BusinessLogicImportRequest, db: Session = Depends(get_db)):
    try:
        return await logic_import_service.import_from_code(
            db,
            domain_id=data.domain_id,
            code=data.code,
            source_type=data.source_type,
            operator=data.operator,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/business-logics/{logic_id}", response_model=BusinessLogicDetail)
def update_business_logic(
    logic_id: str, data: BusinessLogicUpdate, db: Session = Depends(get_db)
):
    try:
        return edit_service.update_business_logic(
            db,
            logic_id,
            display_name=data.display_name,
            description=data.description,
            logic_type=data.logic_type,
            expression_summary=data.expression_summary,
            expression_draft=data.expression_draft,
            expression_json=data.expression_json,
            category_id=data.category_id,
            operator=data.operator,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/business-logics/{logic_id}/pre-publish", response_model=BusinessLogicOut)
def pre_publish_business_logic(logic_id: str, db: Session = Depends(get_db)):
    try:
        return edit_service.pre_publish_business_logic(db, logic_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/business-logics/{logic_id}/publish", response_model=ConfirmationOut)
def publish_business_logic(logic_id: str, db: Session = Depends(get_db)):
    logic = db.get(BusinessLogic, logic_id)
    if not logic:
        raise HTTPException(status_code=404, detail="Business logic not found")
    confirmation = confirmation_service.create(
        db,
        ConfirmationCreate(
            ontology_id=logic.ontology_id,
            target_type="business_logic",
            target_id=logic.id,
            action_type="publish",
        ),
    )
    return confirmation_service.confirm(db, confirmation.id)


@router.delete("/business-logics/{logic_id}")
def delete_business_logic(logic_id: str, db: Session = Depends(get_db)):
    logic = db.get(BusinessLogic, logic_id)
    if not logic:
        raise HTTPException(status_code=404, detail="Business logic not found")
    try:
        confirmation = confirmation_service.create(
            db,
            ConfirmationCreate(
                ontology_id=logic.ontology_id,
                target_type="business_logic",
                target_id=logic.id,
                action_type="delete",
            ),
        )
        confirmation_service.confirm(db, confirmation.id)
        return {"id": logic_id, "deleted": True}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
