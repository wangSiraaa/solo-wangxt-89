"""Application configuration.

Default DSN points at the local, unprivileged PostgreSQL instance used in
development/test.  Override DATABASE_URL for any real deployment.
"""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = (
        "postgresql+psycopg2://timing@/timing?host=/tmp&port=55432"
    )
    # Duplicate reads closer together than this (same chip, same node) are
    # physically meaningless (athlete standing on the mat) and are collapsed.
    repeat_read_window_s: float = 8.0


settings = Settings()
