"""Settings read from environment variables, validated once at startup."""

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(frozen=True)

    database_url: str
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"


def load_settings() -> Settings:
    """Build settings from the environment; fails fast if a required value is missing."""
    return Settings()
