"""Aggregate management API routers."""

from fastapi import APIRouter

from app.api import (
    business_logic,
    chat_bi,
    confirmations,
    ontology,
    settings,
    workspace,
)

router = APIRouter()
router.include_router(settings.router)
router.include_router(workspace.router)
router.include_router(ontology.router)
router.include_router(business_logic.router)
router.include_router(confirmations.router)
router.include_router(chat_bi.router)
