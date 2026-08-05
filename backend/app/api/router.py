"""Aggregate management API routers."""

from fastapi import APIRouter

from app.api import (
    agents,
    business_logic,
    chat_bi,
    confirmations,
    data_app,
    governance,
    ontology,
    principals,
    public_routes,
    settings,
    warehouse,
    workspace,
)

router = APIRouter()
router.include_router(settings.router)
router.include_router(workspace.router)
router.include_router(ontology.router)
router.include_router(business_logic.router)
router.include_router(confirmations.router)
router.include_router(chat_bi.router)
router.include_router(data_app.router)
router.include_router(warehouse.router)
router.include_router(principals.router)
router.include_router(agents.router)
router.include_router(governance.router)
router.include_router(public_routes.router)
