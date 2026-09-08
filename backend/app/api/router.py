"""Aggregate management API routers."""

from fastapi import APIRouter

from app.api import (
    agents,
    business_logic,
    confirmations,
    datasources,
    dependencies,
    dimensional_model,
    governance,
    lineage,
    mcp,
    modeling,
    ontology,
    principals,
    settings,
    superset,
    warehouse,
    warehouse_migration,
    workspace,
)

router = APIRouter()
router.include_router(settings.router)
router.include_router(dependencies.router)
router.include_router(workspace.router)
router.include_router(ontology.router)
router.include_router(business_logic.router)
router.include_router(confirmations.router)
router.include_router(datasources.router)
router.include_router(warehouse.router)
router.include_router(warehouse_migration.router)
router.include_router(principals.router)
router.include_router(mcp.router)
router.include_router(superset.router)
router.include_router(agents.router)
router.include_router(governance.router)
router.include_router(lineage.router)
router.include_router(modeling.router)
router.include_router(dimensional_model.router)
