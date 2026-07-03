from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.config import settings as env_settings
from app.models import DatahubSetting, LlmServiceConfig

DEEPSEEK_MODELS = [
    {
        "id": "deepseek-v4-flash",
        "label": "deepseek-v4-flash",
        "description": "DeepSeek-V4-Flash · 高性价比默认模型，1M 上下文，支持思考/非思考模式",
        "deprecated": False,
    },
    {
        "id": "deepseek-v4-pro",
        "label": "deepseek-v4-pro",
        "description": "DeepSeek-V4-Pro · 旗舰模型，1M 上下文，适合复杂推理与 Agent 任务",
        "deprecated": False,
    },
    {
        "id": "deepseek-chat",
        "label": "deepseek-chat",
        "description": "兼容模型 · 等同 V4-Flash 非思考模式（2026/07/24 弃用）",
        "deprecated": True,
    },
    {
        "id": "deepseek-reasoner",
        "label": "deepseek-reasoner",
        "description": "兼容模型 · 等同 V4-Flash 思考模式（2026/07/24 弃用）",
        "deprecated": True,
    },
]


@dataclass
class DatahubRuntimeConfig:
    gms_url: str
    frontend_url: str
    token: str | None
    use_mock: bool


@dataclass
class LlmRuntimeConfig:
    api_base_url: str
    api_key: str | None
    model: str
    use_mock: bool


def mask_secret(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) <= 4:
        return "****"
    return f"{'*' * (len(value) - 4)}{value[-4:]}"


class SettingsService:
    def list_llm_models(self) -> list[dict]:
        return DEEPSEEK_MODELS

    def list_llm_services(self, db: Session) -> list[LlmServiceConfig]:
        self.ensure_defaults(db)
        return (
            db.query(LlmServiceConfig)
            .order_by(LlmServiceConfig.is_default.desc(), LlmServiceConfig.updated_at.desc())
            .all()
        )

    def get_llm_service(self, db: Session, service_id: str) -> LlmServiceConfig | None:
        return db.get(LlmServiceConfig, service_id)

    def create_llm_service(self, db: Session, data: dict) -> LlmServiceConfig:
        self.ensure_defaults(db)
        if data.get("is_default"):
            self._clear_default_llm(db)
        service = LlmServiceConfig(**data)
        db.add(service)
        db.commit()
        db.refresh(service)
        if not db.query(LlmServiceConfig).filter(LlmServiceConfig.is_default.is_(True)).first():
            service.is_default = True
            db.commit()
            db.refresh(service)
        return service

    def update_llm_service(
        self, db: Session, service_id: str, data: dict
    ) -> LlmServiceConfig | None:
        service = db.get(LlmServiceConfig, service_id)
        if not service:
            return None
        if data.get("is_default"):
            self._clear_default_llm(db)
        for key, value in data.items():
            if key == "api_key" and value is None:
                continue
            setattr(service, key, value)
        db.commit()
        db.refresh(service)
        return service

    def delete_llm_service(self, db: Session, service_id: str) -> bool:
        service = db.get(LlmServiceConfig, service_id)
        if not service:
            return False
        was_default = service.is_default
        db.delete(service)
        db.commit()
        if was_default:
            fallback = db.query(LlmServiceConfig).order_by(LlmServiceConfig.updated_at.desc()).first()
            if fallback:
                fallback.is_default = True
                db.commit()
        return True

    def get_datahub_settings(self, db: Session) -> DatahubSetting:
        self.ensure_defaults(db)
        row = db.get(DatahubSetting, "default")
        assert row is not None
        return row

    def update_datahub_settings(self, db: Session, data: dict) -> DatahubSetting:
        row = self.get_datahub_settings(db)
        for key, value in data.items():
            if key == "token" and value is None:
                continue
            setattr(row, key, value)
        db.commit()
        db.refresh(row)
        return row

    def get_datahub_runtime(self, db: Session) -> DatahubRuntimeConfig:
        row = self.get_datahub_settings(db)
        return DatahubRuntimeConfig(
            gms_url=row.gms_url,
            frontend_url=row.frontend_url,
            token=row.token,
            use_mock=row.use_mock,
        )

    def get_llm_runtime(self, db: Session) -> LlmRuntimeConfig:
        self.ensure_defaults(db)
        service = (
            db.query(LlmServiceConfig)
            .filter(LlmServiceConfig.is_default.is_(True), LlmServiceConfig.enabled.is_(True))
            .first()
        )
        if not service:
            service = db.query(LlmServiceConfig).filter(LlmServiceConfig.enabled.is_(True)).first()
        if service:
            use_mock = service.use_mock or not service.api_key
            return LlmRuntimeConfig(
                api_base_url=service.api_base_url,
                api_key=service.api_key,
                model=service.model,
                use_mock=use_mock,
            )
        return LlmRuntimeConfig(
            api_base_url="https://api.deepseek.com",
            api_key=env_settings.openai_api_key,
            model=env_settings.openai_model,
            use_mock=env_settings.use_mock_llm or not env_settings.openai_api_key,
        )

    def ensure_defaults(self, db: Session) -> None:
        if not db.get(DatahubSetting, "default"):
            db.add(
                DatahubSetting(
                    id="default",
                    gms_url=env_settings.datahub_gms_url,
                    frontend_url=env_settings.datahub_frontend_url,
                    token=env_settings.datahub_token,
                    use_mock=env_settings.use_mock_datahub,
                )
            )
            db.commit()

        if db.query(LlmServiceConfig).count() == 0:
            db.add(
                LlmServiceConfig(
                    name="DeepSeek 默认",
                    provider="deepseek",
                    api_base_url="https://api.deepseek.com",
                    api_key=env_settings.openai_api_key,
                    model="deepseek-v4-flash",
                    is_default=True,
                    enabled=True,
                    use_mock=env_settings.use_mock_llm,
                )
            )
            db.commit()

    def _clear_default_llm(self, db: Session) -> None:
        for item in db.query(LlmServiceConfig).filter(LlmServiceConfig.is_default.is_(True)).all():
            item.is_default = False
