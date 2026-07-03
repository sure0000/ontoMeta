from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    app_name: str = "ontoMeta"
    debug: bool = True
    database_url: str = "sqlite:///./ontometa.db"

    datahub_gms_url: str = "http://localhost:8080"
    datahub_frontend_url: str = "http://localhost:9002"
    datahub_token: str | None = None
    use_mock_datahub: bool = False

    openai_api_key: str | None = None
    openai_model: str = "gpt-4o-mini"
    use_mock_llm: bool = True

    cors_origins: list[str] = ["http://localhost:5173", "http://localhost:3000"]


settings = Settings()
