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

    # 草稿生成时单次 LLM 证据 payload 的字符预算：超过则自动分块 Map-Reduce。
    # 用字符长度做保守估计（宁可多切一块也不冒超长风险），按模型上下文调优：
    # DeepSeek 64K token 上下文，此处默认 ~48000 字符（约 16-20K token），
    # 为 system prompt 与输出预留充足余量。
    llm_context_budget_chars: int = 48000
    # 分块生成时并发调用 LLM 的子块数上限。
    draft_chunk_max_concurrency: int = 2
    # 分块生成时每批最多打包的表(对象)数：优先按表数切块，字符预算作为兜底细分。
    draft_chunk_table_batch_size: int = 10

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
