from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    access_token: str = ""
    upstream_base_url: str = "https://opencode.ai/zen/v1"
    opencode_api_key: str = ""
    data_dir: Path = Path("./data")
    host: str = "0.0.0.0"
    port: int = 8080
    use_direct: bool = True
    request_timeout: float = 120.0
    models_cache_ttl: float = 300.0

    @property
    def db_path(self) -> Path:
        return self.data_dir / "ocfreeproxy.db"


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return settings
