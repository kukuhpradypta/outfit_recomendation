"""Application configuration loaded from environment variables."""
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # PostgreSQL
    pg_host: str = "localhost"
    pg_port: int = 5432
    pg_user: str = "postgres"
    pg_password: str = "password"
    pg_db_name: str = "outfit_recomendation"

    # Gemini
    gemini_api_key: str = ""
    gemini_text_model: str = "gemini-flash-latest"
    gemini_image_model: str = "gemini-2.5-flash-image"
    # Reasoning depth for Gemini 3.x models: minimal/low/medium/high. Lower is
    # faster. Leave empty to use the model default.
    gemini_thinking_level: str = "low"
    # Per-request timeout so a slow Gemini call never hangs the API.
    gemini_timeout_sec: int = 30
    # Max candidate items per category sent to Gemini (keeps the prompt small).
    reco_max_per_category: int = 8

    # App
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    public_dir: str = "public"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+psycopg2://{self.pg_user}:{self.pg_password}"
            f"@{self.pg_host}:{self.pg_port}/{self.pg_db_name}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
