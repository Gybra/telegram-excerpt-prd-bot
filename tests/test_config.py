"""Settings validation tests."""

from __future__ import annotations

import os

import pytest

from telegram_excerpt.config import Mode, get_settings


def test_polling_mode_loads() -> None:
    s = get_settings()
    assert s.mode is Mode.POLLING
    assert s.forward_chat_id == 999


def test_webhook_mode_requires_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODE", "webhook")
    monkeypatch.delenv("BASE_URL", raising=False)
    get_settings.cache_clear()
    with pytest.raises(ValueError, match="BASE_URL"):
        get_settings()


def test_webhook_mode_requires_https(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODE", "webhook")
    monkeypatch.setenv("BASE_URL", "http://insecure.example.com")
    monkeypatch.setenv("SCHEDULER_AUTH_TOKEN", "x" * 32)
    get_settings.cache_clear()
    with pytest.raises(ValueError, match="https"):
        get_settings()


def test_webhook_mode_requires_scheduler_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MODE", "webhook")
    monkeypatch.setenv("BASE_URL", "https://foo.run.app")
    monkeypatch.delenv("SCHEDULER_AUTH_TOKEN", raising=False)
    get_settings.cache_clear()
    with pytest.raises(ValueError, match="SCHEDULER_AUTH_TOKEN"):
        get_settings()


def test_webhook_mode_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODE", "webhook")
    monkeypatch.setenv("BASE_URL", "https://foo.run.app/")
    monkeypatch.setenv("SCHEDULER_AUTH_TOKEN", "x" * 32)
    get_settings.cache_clear()
    s = get_settings()
    assert s.mode is Mode.WEBHOOK
    assert s.base_url == "https://foo.run.app"  # trailing slash stripped


def test_default_n_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEFAULT_N", "0")
    get_settings.cache_clear()
    with pytest.raises(ValueError, match="DEFAULT_N"):
        get_settings()
    monkeypatch.setenv("DEFAULT_N", "50")
    get_settings.cache_clear()
    assert get_settings().default_n == 50


def test_secret_not_in_repr() -> None:
    s = get_settings()
    r = repr(s)
    assert "sk-test" not in r
    assert os.environ["TELEGRAM_ADMIN_BOT_TOKEN"] not in r
