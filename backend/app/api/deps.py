"""Shared service singletons for API routers."""

from app.services.agent_pipeline import AgentPipelineService
from app.services.chat_bi import ChatBiService
from app.services.data_app import DataAppService
from app.services.datahub_writeback import DataHubWritebackService
from app.services.edit import EditService
from app.services.expression_formatter import ExpressionFormatterService
from app.services.lineage_emitter import LineageEmitter
from app.services.logic_import import LogicImportService
from app.services.materialization_contract import MaterializationContractService
from app.services.ontology_revision import OntologyRevisionService
from app.services.provenance_service import ProvenanceService
from app.services.principal_service import PrincipalService
from app.services.publish import ConfirmationService
from app.services.query import OntologyQueryService, WorkspaceService
from app.services.settings_service import SettingsService
from app.services.task_pipeline import TaskPipelineService
from app.services.warehouse_generator import WarehouseGenerator

workspace = WorkspaceService()
query = OntologyQueryService()
confirmation_service = ConfirmationService()
edit_service = EditService()
settings_service = SettingsService()
logic_import_service = LogicImportService()
provenance_service = ProvenanceService()
revision_service = OntologyRevisionService()
expression_formatter_service = ExpressionFormatterService()
chat_bi_service = ChatBiService()
data_app_service = DataAppService()
materialization_contract_service = MaterializationContractService()
warehouse_generator = WarehouseGenerator()
principal_service = PrincipalService()
agent_pipeline = AgentPipelineService()
task_pipeline = TaskPipelineService()
datahub_writeback = DataHubWritebackService()
lineage_emitter = LineageEmitter()
