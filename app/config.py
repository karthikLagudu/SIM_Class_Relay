from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Class Relay"
    environment: str = "development"
    demo_mode: bool = False
    database_url: str = "sqlite:///./classroom_companion.db"
    session_secret: str = "development-only-change-me"
    base_url: str = "http://127.0.0.1:8767"
    school_timezone: str = "Asia/Kolkata"
    llm_mode: str = "fake"
    openai_api_key: str | None = None
    llm_confidence_threshold: float = Field(default=0.75, ge=0, le=1)
    openai_model: str = "gpt-5-mini"
    speech_mode: Literal["off", "openai"] = "off"
    openai_transcription_model: str = "gpt-transcribe"
    telegram_mode: str = "log"
    telegram_updates: Literal["webhook", "polling"] = "webhook"
    telegram_bot_token: str | None = None
    telegram_bot_username: str | None = None
    telegram_webhook_secret: str = "change-me"
    telegram_link_token_minutes: int = Field(default=30, ge=5, le=1440)
    upload_dir: Path = Path("uploads")
    max_upload_bytes: int = Field(default=10 * 1024 * 1024, ge=1)
    quiet_hour_start: int = Field(default=22, ge=0, le=23)
    quiet_hour_end: int = Field(default=7, ge=0, le=23)
    reminder_interval_seconds: int = Field(default=45, ge=0)
    conversation_context_minutes: int = Field(default=30, ge=1, le=1440)
    notification_max_attempts: int = Field(default=3, ge=1, le=10)

    @field_validator("llm_mode")
    @classmethod
    def validate_llm_mode(cls, value: str) -> str:
        if value not in {"real", "fake"}:
            raise ValueError("LLM_MODE must be either 'real' or 'fake'")
        return value

    @field_validator("telegram_mode")
    @classmethod
    def validate_telegram_mode(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"log", "real"}:
            raise ValueError("TELEGRAM_MODE must be either 'log' or 'real'")
        return normalized

    @field_validator("telegram_bot_username")
    @classmethod
    def normalize_bot_username(cls, value: str | None) -> str | None:
        normalized = (value or "").strip().lstrip("@")
        return normalized or None

    @model_validator(mode="after")
    def validate_deployment_credentials(self) -> Settings:
        if self.environment == "production" and (
            len(self.session_secret) < 32 or self.session_secret == "replace-with-a-long-random-secret"
        ):
            raise ValueError("Set a long random SESSION_SECRET for production")
        if self.telegram_mode == "real" and not self.telegram_bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN is required when TELEGRAM_MODE=real")
        if self.speech_mode == "openai" and not self.openai_api_key:
            raise ValueError("OPENAI_API_KEY is required when SPEECH_MODE=openai")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
