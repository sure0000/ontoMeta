"""Shared service singletons for API routers."""

from app.services.chat_bi import ChatBiService
from app.services.edit import EditService
from app.services.expression_formatter import ExpressionFormatterService
from app.services.logic_import import LogicImportService
from app.services.publish import ConfirmationService
from app.services.query import OntologyQueryService, WorkspaceService
from app.services.settings_service import SettingsService

workspace = WorkspaceService()
query = OntologyQueryService()
confirmation_service = ConfirmationService()
edit_service = EditService()
settings_service = SettingsService()
logic_import_service = LogicImportService()
expression_formatter_service = ExpressionFormatterService()
chat_bi_service = ChatBiService()
