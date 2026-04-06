"""Shared pytest fixtures.

Forces env vars for tests (avoiding the need for a real .env) and provides
factories for ``BotConfig``/``BufferedMessage``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

# ─── Env setup (must run BEFORE the package is imported) ──────────────
_TEST_ENV = {
    "TELEGRAM_ADMIN_BOT_TOKEN": "123:TEST-ADMIN-TOKEN",
    "FORWARD_CHAT_ID": "999",
    "OPENROUTER_API_KEY": "sk-test",
    "OPENROUTER_MODEL": "test/model",
    "GOOGLE_APPLICATION_CREDENTIALS": "/tmp/fake.json",  # noqa: S108
    "FIRESTORE_PROJECT_ID": "test-project",
    "MODE": "polling",
    "BATCH_SILENCE_SECONDS": "180",
    "DEFAULT_N": "50",
    "SCHEDULER_AUTH_TOKEN": "t" * 32,
}
for k, v in _TEST_ENV.items():
    os.environ[k] = v


@pytest.fixture(autouse=True)
def _reset_settings_cache() -> Iterator[None]:
    """Reset get_settings() cache for every test."""
    from telegram_excerpt.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def utc_now() -> datetime:
    return datetime.now(UTC)


@pytest.fixture
def make_message() -> Any:
    """Factory producing ``BufferedMessage`` objects."""
    from telegram_excerpt.models import BufferedMessage

    def _make(
        message_id: int = 1,
        chat_id: int = -100123,
        text: str = "hello",
        user_name: str = "Mario",
        ts: datetime | None = None,
    ) -> BufferedMessage:
        return BufferedMessage(
            message_id=message_id,
            chat_id=chat_id,
            user_id=42,
            user_name=user_name,
            text=text,
            ts=ts or datetime.now(UTC),
        )

    return _make


@pytest.fixture
def make_bot_config() -> Any:
    """Factory producing ``BotConfig`` objects."""
    from telegram_excerpt.models import BotConfig, compute_token_hash

    def _make(
        chat_id: int = -100123,
        n: int = 50,
        last_read_message_id: int = 0,
        chat_title: str = "Test Group",
    ) -> BotConfig:
        token = f"{abs(chat_id)}:FAKE"
        return BotConfig(
            token=token,
            token_hash=compute_token_hash(token),
            chat_id=chat_id,
            chat_title=chat_title,
            n=n,
            last_read_message_id=last_read_message_id,
        )

    return _make
