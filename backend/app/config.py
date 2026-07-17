from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    app_name: str = "ontoMeta"
    debug: bool = True
    database_url: str = "sqlite:///./ontometa.db"

    # 管理端共享 Token；未配置时受保护的 /api 返回 503
    ontometa_admin_token: str | None = None
    # 外部 API Key 哈希 pepper（可选，变更后须重新生成全部 App Key）
    api_key_hash_pepper: str | None = None

    datahub_gms_url: str = "http://localhost:8080"
    datahub_frontend_url: str = "http://localhost:9002"
    datahub_token: str | None = None
    use_mock_datahub: bool = False

    openai_api_key: str | None = None
    openai_model: str = "gpt-4o-mini"
    use_mock_llm: bool = True
    llm_timeout_seconds: float = 300.0

    max_concurrent_draft_generations: int = 2
    datahub_max_concurrency: int = 5

    # 外部 API / MCP：每应用每分钟默认请求上限（进程内固定窗口；<=0 关闭）
    external_api_rate_limit_per_minute: int = 60

    cors_origins: list[str] = [
        "http://localhost:5180",
        "http://localhost:5173",
        "http://localhost:3000",
    ]


settings = Settings()
