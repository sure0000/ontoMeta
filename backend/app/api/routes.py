import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import BusinessLogic, ObjectType
from app.schemas import (
    BusinessLogicCreate,
    BusinessLogicDetail,
    BusinessLogicImportRequest,
    BusinessLogicObjectBindingCreate,
    BusinessLogicObjectBindingOut,
    BusinessLogicOut,
    BusinessLogicPropertyBindingCreate,
    BusinessLogicPropertyBindingOut,
    BusinessLogicUpdate,
    ChangeLogOut,
    ConfirmationCreate,
    ConfirmationOut,
    DataHubDatasetOption,
    DatahubSettingsOut,
    DatahubSettingsUpdate,
    DomainContextDetail,
    DomainContextSummary,
    DraftProgressOut,
    EnsureObjectTypeRequest,
    ExpressionFormatRequest,
    ExpressionFormatResponse,
    LlmModelOption,
    LlmServiceConfigCreate,
    LlmServiceConfigDetail,
    LlmServiceConfigOut,
    LlmServiceConfigUpdate,
    ObjectTypeDetail,
    ObjectTypeSummary,
    ObjectTypeUpdate,
    OntologyGraph,
    OntologySummary,
    PropertyOut,
    PropertyUpdate,
    RelationTypeCreate,
    RelationTypeDetail,
    RelationTypeOut,
    RelationTypeUpdate,
    TaskRecordOut,
    VersionRecordOut,
)
from app.services.edit import EditService
from app.services.expression_formatter import ExpressionFormatterService
from app.services.logic_import import LogicImportService
from app.services.publish import ConfirmationService
from app.services.query import OntologyQueryService, WorkspaceService
from app.services.settings_service import SettingsService, mask_secret

router = APIRouter()
workspace = WorkspaceService()
query = OntologyQueryService()
confirmation_service = ConfirmationService()
edit_service = EditService()
settings_service = SettingsService()
logic_import_service = LogicImportService()
expression_formatter_service = ExpressionFormatterService()


def _llm_service_out(service) -> LlmServiceConfigOut:
    return LlmServiceConfigOut(
        id=service.id,
        name=service.name,
        provider=service.provider,
        api_base_url=service.api_base_url,
        model=service.model,
        is_default=service.is_default,
        enabled=service.enabled,
        use_mock=service.use_mock,
        api_key_set=bool(service.api_key),
        api_key_hint=mask_secret(service.api_key),
        created_at=service.created_at,
        updated_at=service.updated_at,
    )


def _llm_service_detail(service) -> LlmServiceConfigDetail:
    return LlmServiceConfigDetail(
        **_llm_service_out(service).model_dump(),
        api_key=service.api_key,
    )


def _datahub_settings_out(row) -> DatahubSettingsOut:
    return DatahubSettingsOut(
        gms_url=row.gms_url,
        frontend_url=row.frontend_url,
        token_set=bool(row.token),
        token_hint=mask_secret(row.token),
        use_mock=row.use_mock,
        updated_at=row.updated_at,
    )


@router.get("/config")
def get_app_config(db: Session = Depends(get_db)):
    datahub = settings_service.get_datahub_runtime(db)
    return {
        "datahub_gms_url": datahub.gms_url,
        "datahub_frontend_url": datahub.frontend_url,
    }


@router.get("/settings/llm-models", response_model=list[LlmModelOption])
def list_llm_models():
    return settings_service.list_llm_models()


@router.get("/settings/llm-services", response_model=list[LlmServiceConfigOut])
def list_llm_services(db: Session = Depends(get_db)):
    return [_llm_service_out(item) for item in settings_service.list_llm_services(db)]


@router.post("/settings/llm-services", response_model=LlmServiceConfigDetail)
def create_llm_service(data: LlmServiceConfigCreate, db: Session = Depends(get_db)):
    service = settings_service.create_llm_service(db, data.model_dump())
    return _llm_service_detail(service)


@router.get("/settings/llm-services/{service_id}", response_model=LlmServiceConfigDetail)
def get_llm_service(service_id: str, db: Session = Depends(get_db)):
    service = settings_service.get_llm_service(db, service_id)
    if not service:
        raise HTTPException(status_code=404, detail="LLM 服务配置不存在")
    return _llm_service_detail(service)


@router.put("/settings/llm-services/{service_id}", response_model=LlmServiceConfigDetail)
def update_llm_service(
    service_id: str, data: LlmServiceConfigUpdate, db: Session = Depends(get_db)
):
    payload = data.model_dump(exclude_unset=True)
    service = settings_service.update_llm_service(db, service_id, payload)
    if not service:
        raise HTTPException(status_code=404, detail="LLM 服务配置不存在")
    return _llm_service_detail(service)


@router.delete("/settings/llm-services/{service_id}")
def delete_llm_service(service_id: str, db: Session = Depends(get_db)):
    if not settings_service.delete_llm_service(db, service_id):
        raise HTTPException(status_code=404, detail="LLM 服务配置不存在")
    return {"id": service_id, "deleted": True}


@router.get("/settings/datahub", response_model=DatahubSettingsOut)
def get_datahub_settings(db: Session = Depends(get_db)):
    return _datahub_settings_out(settings_service.get_datahub_settings(db))


@router.put("/settings/datahub", response_model=DatahubSettingsOut)
def update_datahub_settings(data: DatahubSettingsUpdate, db: Session = Depends(get_db)):
    row = settings_service.update_datahub_settings(db, data.model_dump())
    return _datahub_settings_out(row)



@router.get("/datahub/datasets", response_model=list[DataHubDatasetOption])
async def search_datahub_datasets(
    query: str = Query(""),
    ontology_id: str | None = Query(None),
    db: Session = Depends(get_db),
):
    """搜索 DataHub datasets。

    若提供 ontology_id，会在结果中标注该 dataset 是否已映射为本体下的 ObjectType。
    """
    from app.connectors.datahub import DataHubConnector

    connector = DataHubConnector(settings_service.get_datahub_runtime(db))
    datasets = await connector.search_datasets(query)

    options: list[DataHubDatasetOption] = []
    for ds in datasets:
        object_type_id = None
        object_type_display_name = None
        if ontology_id:
            existing = (
                db.query(ObjectType)
                .filter(
                    ObjectType.ontology_id == ontology_id,
                    ObjectType.source_ref == ds.urn,
                )
                .first()
            )
            if existing:
                object_type_id = existing.id
                object_type_display_name = existing.display_name
        options.append(
            DataHubDatasetOption(
                urn=ds.urn,
                name=ds.name,
                display_name=ds.display_name,
                description=ds.description,
                platform=ds.platform,
                container=ds.container,
                object_type_id=object_type_id,
                object_type_display_name=object_type_display_name,
                datahub_url=connector.get_dataset_url(ds.urn),
            )
        )
    return options


@router.post("/object-types/ensure", response_model=ObjectTypeSummary)
async def ensure_object_type_from_dataset(
    data: EnsureObjectTypeRequest,
    db: Session = Depends(get_db),
):
    """根据 DataHub dataset urn 查找或创建对应 ObjectType。"""
    try:
        return await edit_service.ensure_object_type_from_dataset(
            db,
            ontology_id=data.ontology_id,
            dataset_urn=data.dataset_urn,
            operator=data.operator,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/domains", response_model=list[DomainContextSummary])
async def list_domains(db: Session = Depends(get_db)):
    try:
        return await workspace.sync_domains(db)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"无法从 DataHub 同步数据域，请检查 DataHub 连接配置：{exc}",
        ) from exc


@router.get("/domains/{domain_id}", response_model=DomainContextDetail)
def get_domain(domain_id: str, db: Session = Depends(get_db)):
    detail = workspace.get_domain(db, domain_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Domain not found")
    return detail


@router.post("/domains/{domain_id}/generate-draft", response_model=DraftProgressOut)
async def generate_draft(domain_id: str, db: Session = Depends(get_db)):
    try:
        progress = workspace.start_draft_generation(db, domain_id)
        asyncio.create_task(workspace._run_draft_generation(domain_id, progress.task_id))
        return progress
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/domains/{domain_id}/progress", response_model=DraftProgressOut)
def get_progress(domain_id: str, db: Session = Depends(get_db)):
    progress = workspace.get_progress(db, domain_id)
    if not progress:
        raise HTTPException(status_code=404, detail="No generation task found")
    return progress


@router.get("/domains/{domain_id}/tasks", response_model=list[TaskRecordOut])
def list_domain_tasks(domain_id: str, db: Session = Depends(get_db)):
    domain = workspace.get_domain(db, domain_id)
    if not domain:
        raise HTTPException(status_code=404, detail="Domain not found")
    return workspace.list_tasks(db, domain_id)


@router.get("/domains/{domain_id}/tasks/{task_id}/logs", response_model=list[ChangeLogOut])
def get_task_logs(domain_id: str, task_id: str, db: Session = Depends(get_db)):
    try:
        return workspace.get_task_logs(db, domain_id, task_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


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


@router.get("/ontologies/{ontology_id}/object-types", response_model=list[ObjectTypeSummary])
def list_object_types_by_ontology(ontology_id: str, db: Session = Depends(get_db)):
    return query.list_object_types(db, ontology_id=ontology_id)


@router.get("/ontologies/{ontology_id}/graph", response_model=OntologyGraph)
def get_ontology_graph(ontology_id: str, db: Session = Depends(get_db)):
    return query.get_ontology_graph(db, ontology_id)


@router.get("/ontologies/{ontology_id}/versions", response_model=list[VersionRecordOut])
def list_ontology_versions(ontology_id: str, db: Session = Depends(get_db)):
    return query.list_versions(db, ontology_id)


@router.get("/object-types", response_model=list[ObjectTypeSummary])
def list_object_types(
    ontology_id: str | None = Query(None),
    domain_id: str | None = Query(None),
    published_only: bool = Query(False),
    db: Session = Depends(get_db),
):
    return query.list_object_types(
        db,
        ontology_id=ontology_id,
        domain_context_id=domain_id,
        published_only=published_only,
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


@router.get("/ontologies/{ontology_id}/relation-types", response_model=list[RelationTypeOut])
def list_relation_types_by_ontology(ontology_id: str, db: Session = Depends(get_db)):
    return query.list_relation_types(db, ontology_id=ontology_id)


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


@router.get("/relation-types", response_model=list[RelationTypeOut])
def list_relation_types(
    ontology_id: str | None = Query(None),
    domain_id: str | None = Query(None),
    published_only: bool = Query(False),
    db: Session = Depends(get_db),
):
    return query.list_relation_types(
        db,
        ontology_id=ontology_id,
        domain_context_id=domain_id,
        published_only=published_only,
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


@router.get("/business-logics", response_model=list[BusinessLogicOut])
def list_business_logics(
    ontology_id: str | None = Query(None),
    domain_id: str | None = Query(None),
    published_only: bool = Query(False),
    db: Session = Depends(get_db),
):
    return query.list_business_logics(
        db,
        ontology_id=ontology_id,
        domain_context_id=domain_id,
        published_only=published_only,
    )


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
            operator=data.operator,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post(
    "/business-logics/format-expression",
    response_model=ExpressionFormatResponse,
)
def format_expression(data: ExpressionFormatRequest, db: Session = Depends(get_db)):
    try:
        return expression_formatter_service.format(
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


@router.post("/confirmations", response_model=ConfirmationOut)
def create_confirmation(data: ConfirmationCreate, db: Session = Depends(get_db)):
    return confirmation_service.create(db, data)


@router.get("/confirmations/{confirmation_id}", response_model=ConfirmationOut)
def get_confirmation(confirmation_id: str, db: Session = Depends(get_db)):
    item = confirmation_service.get(db, confirmation_id)
    if not item:
        raise HTTPException(status_code=404, detail="Confirmation not found")
    return item


@router.post("/confirmations/{confirmation_id}/confirm", response_model=ConfirmationOut)
def confirm_action(confirmation_id: str, db: Session = Depends(get_db)):
    try:
        return confirmation_service.confirm(db, confirmation_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/confirmations/{confirmation_id}/cancel", response_model=ConfirmationOut)
def cancel_action(confirmation_id: str, db: Session = Depends(get_db)):
    try:
        return confirmation_service.cancel(db, confirmation_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
