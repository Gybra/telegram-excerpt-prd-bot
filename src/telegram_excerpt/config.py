"""Typed runtime configuration loaded from environment variables.

This module centralizes the reading and validation of every environment
variable needed by the system. It uses pydantic-settings to validate
at-startup: if a required variable is missing or malformed, the process
fails immediately with a clear message.

Example:
    >>> from telegram_excerpt.config import get_settings
    >>> settings = get_settings()
    >>> settings.mode
    'polling'
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Mode(StrEnum):
    """Application runtime mode."""

    POLLING = "polling"
    WEBHOOK = "webhook"


class Settings(BaseSettings):
    """Configuration loaded from env vars and .env file.

    All variables are case-insensitive. ``SecretStr`` fields are never
    printed in full in logs/repr.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ─── Telegram ────────────────────────────────────────────────────
    telegram_admin_bot_token: SecretStr = Field(
        ..., description="Admin bot token (command-based management)."
    )
    forward_chat_id: int = Field(..., description="Admin's private chat ID (sole PRD recipient).")

    # ─── LLM (OpenRouter) ────────────────────────────────────────────
    openrouter_api_key: SecretStr = Field(..., description="OpenRouter API key.")
    openrouter_model: str = Field(
        default="qwen/qwen3.6-plus:free", description="OpenRouter model id."
    )
    openrouter_base_url: str = Field(default="https://openrouter.ai/api/v1")

    # ─── Firestore ───────────────────────────────────────────────────
    google_application_credentials: Path = Field(
        ..., description="Path to the GCP service-account JSON."
    )
    firestore_project_id: str = Field(..., description="GCP project ID.")

    # ─── Mode ────────────────────────────────────────────────────────
    mode: Mode = Field(default=Mode.POLLING)
    base_url: str | None = Field(
        default=None,
        description="Public HTTPS URL (required when mode=webhook).",
    )

    # ─── Processing ──────────────────────────────────────────────────
    batch_silence_seconds: Annotated[int, Field(ge=30, le=3600)] = 180
    default_n: Annotated[int, Field(ge=1, le=500)] = 50

    # ─── Webhook auth ────────────────────────────────────────────────
    scheduler_auth_token: SecretStr | None = Field(
        default=None, description="Bearer token for /tasks/process and /admin/setup."
    )

    # ─── Scheduler (polling mode) ────────────────────────────────────
    polling_scheduler_interval_seconds: Annotated[int, Field(ge=5, le=300)] = 30

    # ─── Chat responder (optional) ───────────────────────────────────
    chat_responder_enabled: bool = Field(
        default=False,
        description="If true, child bots reply to every group message via LLM.",
    )
    chat_responder_system_prompt: str = Field(
        default=(
            "Sei un assistente amichevole che risponde sempre in italiano, "
            "in modo chiaro e conciso. Rispondi solo se puoi aggiungere "
            "valore; altrimenti resta in silenzio rispondendo con la "
            "stringa esatta SKIP."
        ),
        description="System prompt for the chat responder.",
    )
    chat_responder_model: str | None = Field(
        default=None,
        description=(
            "OpenRouter model for the chat responder. Falls back to OPENROUTER_MODEL if unset."
        ),
    )
    chat_responder_max_tokens: Annotated[int, Field(ge=16, le=2000)] = 400
    chat_responder_rate_limit: Annotated[int, Field(ge=1, le=1000)] = Field(
        default=5,
        description="Max responder replies per user per rate-limit window.",
    )
    chat_responder_rate_window_seconds: Annotated[int, Field(ge=10, le=86400)] = Field(
        default=60,
        description="Rate-limit window in seconds (per user).",
    )
    chat_responder_daily_budget: Annotated[int, Field(ge=0, le=100000)] = Field(
        default=500,
        description="Max responder LLM calls per day (0 = unlimited).",
    )

    @field_validator("scheduler_auth_token", "telegram_admin_bot_token", "openrouter_api_key", mode="before")
    @classmethod
    def _strip_secret_whitespace(cls, v: str | SecretStr | None) -> str | SecretStr | None:
        """Cloud Run may inject secrets with trailing newlines."""
        if isinstance(v, str):
            return v.strip()
        if isinstance(v, SecretStr):
            stripped = v.get_secret_value().strip()
            return SecretStr(stripped) if stripped else v
        return v

    @field_validator("base_url")
    @classmethod
    def _strip_trailing_slash(cls, v: str | None) -> str | None:
        return v.rstrip("/") if v else v

    @model_validator(mode="after")
    def _validate_webhook_requirements(self) -> Settings:
        """Webhook mode requires scheduler_auth_token at startup.

        ``base_url`` is only needed when ``/admin/setup`` is called, so it
        is validated lazily there instead of blocking container startup
        (Cloud Run assigns the URL *after* the first deploy).
        """
        if self.mode is Mode.WEBHOOK:
            if self.base_url and not self.base_url.startswith("https://"):
                raise ValueError("BASE_URL must be https:// for Telegram webhooks")
            if not self.scheduler_auth_token:
                raise ValueError("SCHEDULER_AUTH_TOKEN is required when MODE=webhook")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the singleton ``Settings`` instance.

    The cache guarantees that env is read only once per process. In tests
    call ``get_settings.cache_clear()`` to force a reload.
    """
    return Settings()
