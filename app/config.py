from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    ekt_api_base_url: str = "https://ekt.kz/api"
    ekt_api_username: str = "apiuser"
    ekt_api_password: str = ""
    openai_api_key: str = ""
    openai_model: str = "gpt-6-sol"
    openai_reasoning_effort: str = "none"
    catalog_search_max_pages: int = 8
    pending_cart_ttl_seconds: int = 600
    max_upload_mb: int = 10


@lru_cache
def get_settings() -> Settings:
    return Settings()
