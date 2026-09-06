"""Shared service singletons for API routers."""

from app.services.agent_pipeline import agent_pipeline  # noqa: F401  (再导出)
from app.services.data_app import DataAppService
from app.services.datahub_writeback import DataHubWritebackService
from app.services.edit import EditService
from app.services.expression_formatter import ExpressionFormatterService
from app.services.lineage_emitter import LineageEmitter
from app.services.logic_import import LogicImportService
from app.services.logic_query import OntologyQueryService
from app.services.materialization_contract import MaterializationContractService
from app.services.principal_service import PrincipalService
from app.services.provenance_service import ProvenanceService
from app.services.publish import ConfirmationService, PublishService
from app.services.settings_service import SettingsService
from app.services.warehouse_generator import WarehouseGenerator
from app.services.workspace_service import WorkspaceService

# Chat BI is an optional, legacy router. Keep its singleton lazy so importing
# shared dependencies does not make the whole application depend on the chat
# service once that router is removed.
_legacy_chat_bi_service = None


def get_legacy_chat_bi_service():
    global _legacy_chat_bi_service
    if _legacy_chat_bi_service is None:
        from app.services.chat_bi import ChatBiService

        _legacy_chat_bi_service = ChatBiService()
    return _legacy_chat_bi_service


def __getattr__(name: str):
    # Backward-compatible import for tests and external integrations. Resolve
    # it only when a caller explicitly asks for the legacy service.
    if name == "chat_bi_service":
        return get_legacy_chat_bi_service()
    raise AttributeError(name)


workspace = WorkspaceService()
query = OntologyQueryService()
confirmation_service = ConfirmationService()
publish_service = PublishService()
edit_service = EditService()
settings_service = SettingsService()
logic_import_service = LogicImportService()
provenance_service = ProvenanceService()
expression_formatter_service = ExpressionFormatterService()
data_app_service = DataAppService()
materialization_contract_service = MaterializationContractService()
warehouse_generator = WarehouseGenerator()
principal_service = PrincipalService()
datahub_writeback = DataHubWritebackService()
lineage_emitter = LineageEmitter()
