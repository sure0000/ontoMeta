from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter

from openai import OpenAI
from sqlalchemy.orm import Session

from app.config import settings as env_settings
from app.models import DatahubSetting, DraftGenerationSetting, LlmServiceConfig, CubeSetting
from app.services.common import make_http_client

# 自建/OpenAI 兼容端点(vLLM、Ollama、LM Studio、企业网关等)常无需鉴权：
# 缺 API Key 时不应回退 Mock，而是用占位符满足 OpenAI SDK 的非空 key 要求。
OPENAI_COMPATIBLE_PROVIDERS = {"openai-compatible"}
OPENAI_COMPATIBLE_PLACEHOLDER_KEY = "EMPTY"

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


@dataclass
class DraftGenerationRuntimeConfig:
    object_chunk_concurrency: int
    relation_chunk_concurrency: int


@dataclass
class CubeRuntimeConfig:
    api_url: str
    api_secret: str | None
    use_mock: bool
    preagg_refresh: str
    tenant_dimension: str | None
    timeout_seconds: int


def mask_secret(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) <= 4:
        return "****"
    return f"{'*' * (len(value) - 4)}{value[-4:]}"


class SettingsService:
    # 进程级缓存：标记默认配置已在数据库中初始化过。
    # 一旦确认存在，后续请求可跳过 ensure_defaults 的探针查询；
    # 当唯一 LLM 行被删除时由 delete_llm_service 重置。
    _defaults_initialized: bool = False

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
        remaining = db.query(LlmServiceConfig).count()
        if remaining == 0:
            # 全部 LLM 配置被删除：下次访问需重新初始化默认项
            SettingsService._defaults_initialized = False
        elif was_default:
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

    def get_draft_generation_settings(self, db: Session) -> DraftGenerationSetting:
        self.ensure_defaults(db)
        row = db.get(DraftGenerationSetting, "default")
        assert row is not None
        return row

    def update_draft_generation_settings(
        self, db: Session, data: dict
    ) -> DraftGenerationSetting:
        row = self.get_draft_generation_settings(db)
        for key, value in data.items():
            setattr(row, key, value)
        db.commit()
        db.refresh(row)
        return row

    def get_draft_generation_runtime(self, db: Session) -> DraftGenerationRuntimeConfig:
        row = self.get_draft_generation_settings(db)
        return DraftGenerationRuntimeConfig(
            object_chunk_concurrency=row.object_chunk_concurrency,
            relation_chunk_concurrency=row.relation_chunk_concurrency,
        )

    def get_cube_settings(self, db: Session) -> CubeSetting:
        self.ensure_defaults(db)
        row = db.get(CubeSetting, "default")
        assert row is not None
        return row

    def update_cube_settings(self, db: Session, data: dict) -> CubeSetting:
        row = self.get_cube_settings(db)
        for key, value in data.items():
            if key == "api_secret" and value is None:
                continue  # 不传则保留原密钥
            setattr(row, key, value)
        db.commit()
        db.refresh(row)
        return row

    def get_cube_runtime(self, db: Session) -> CubeRuntimeConfig:
        row = self.get_cube_settings(db)
        return CubeRuntimeConfig(
            api_url=row.api_url,
            api_secret=row.api_secret,
            use_mock=row.use_mock or not row.api_secret,
            preagg_refresh=row.preagg_refresh,
            tenant_dimension=(row.tenant_dimension or None),
            timeout_seconds=row.timeout_seconds,
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
            provider = (service.provider or "").lower()
            keyless_ok = provider in OPENAI_COMPATIBLE_PROVIDERS
            has_key = bool(service.api_key)
            # 云端提供商缺 Key 仍回退 Mock(安全默认)；自建 OpenAI 兼容端点允许无 Key 直连
            use_mock = service.use_mock or (not has_key and not keyless_ok)
            api_key = service.api_key or (
                OPENAI_COMPATIBLE_PLACEHOLDER_KEY if keyless_ok else None
            )
            return LlmRuntimeConfig(
                api_base_url=service.api_base_url,
                api_key=api_key,
                model=service.model,
                use_mock=use_mock,
            )
        return LlmRuntimeConfig(
            api_base_url="https://api.deepseek.com",
            api_key=env_settings.openai_api_key,
            model=env_settings.openai_model,
            use_mock=env_settings.use_mock_llm or not env_settings.openai_api_key,
        )

    def test_llm_connection(self, db: Session, data: dict) -> dict:
        """真实拨测一个 LLM 配置：发一次最小 chat 请求，返回连通性与耗时。

        编辑态表单常留空 api_key(表示保持原值)，此时用 service_id 取回已存密钥；
        自建 OpenAI 兼容端点无鉴权时用占位符 Key 直连。测试忽略 use_mock —— Mock 无需拨测。
        """
        provider = (data.get("provider") or "").lower()
        keyless_ok = provider in OPENAI_COMPATIBLE_PROVIDERS

        api_key = (data.get("api_key") or "").strip() or None
        if not api_key and data.get("service_id"):
            existing = db.get(LlmServiceConfig, data["service_id"])
            if existing and existing.api_key:
                api_key = existing.api_key
        if not api_key:
            if keyless_ok:
                api_key = OPENAI_COMPATIBLE_PLACEHOLDER_KEY
            else:
                return {"ok": False, "message": "未配置 API Key，无法测试真实连接"}

        base_url = (data.get("api_base_url") or "").strip()
        model = (data.get("model") or "").strip()
        if not base_url or not model:
            return {"ok": False, "message": "缺少模型或 API 地址"}

        client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=min(env_settings.llm_timeout_seconds, 30),
            max_retries=0,
            http_client=make_http_client(),
        )
        start = perf_counter()
        try:
            client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=1,
            )
        except Exception as exc:  # noqa: BLE001 —— 任何异常都视为拨测失败并回显简要原因
            return {"ok": False, "message": f"{type(exc).__name__}: {exc}"[:300]}
        latency_ms = int((perf_counter() - start) * 1000)
        return {"ok": True, "message": "连接成功", "latency_ms": latency_ms, "model": model}

    def ensure_defaults(self, db: Session) -> None:
        # 进程级缓存命中时直接跳过，避免每个请求都跑两个探针查询。
        if SettingsService._defaults_initialized:
            return

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

        if not db.get(DraftGenerationSetting, "default"):
            db.add(
                DraftGenerationSetting(
                    id="default",
                    object_chunk_concurrency=env_settings.draft_chunk_max_concurrency,
                    relation_chunk_concurrency=env_settings.draft_relation_chunk_max_concurrency,
                )
            )
            db.commit()

        if not db.get(CubeSetting, "default"):
            # 首次从环境变量播种一次；此后以 DB（设置页）为权威
            db.add(
                CubeSetting(
                    id="default",
                    api_url=env_settings.cube_api_url,
                    api_secret=env_settings.cube_api_secret,
                    use_mock=env_settings.use_mock_cube,
                    preagg_refresh=env_settings.cube_preagg_refresh,
                    tenant_dimension=env_settings.cube_tenant_dimension,
                    timeout_seconds=int(env_settings.cube_timeout_seconds),
                )
            )
            db.commit()

        SettingsService._defaults_initialized = True

    def _clear_default_llm(self, db: Session) -> None:
        for item in db.query(LlmServiceConfig).filter(LlmServiceConfig.is_default.is_(True)).all():
            item.is_default = False
