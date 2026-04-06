"""BotRegistry and buffer handler tests."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from telegram_excerpt.exceptions import BotAlreadyRegisteredError, BotNotFoundError
from telegram_excerpt.models import BotConfig, BufferedMessage, compute_token_hash

# ─── Helpers ─────────────────────────────────────────────────────────


def _cfg(chat_id: int = -100, n: int = 10) -> BotConfig:
    tok = f"{abs(chat_id)}:FAKE"
    return BotConfig(
        token=tok,
        token_hash=compute_token_hash(tok),
        chat_id=chat_id,
        chat_title="Test",
        n=n,
    )


def _fake_storage(bots: list[BotConfig] | None = None) -> AsyncMock:
    storage = AsyncMock()
    storage.load_bots = AsyncMock(return_value=bots or [])
    storage.append_message = AsyncMock()
    return storage


def _patch_build_app() -> Any:
    """Patch _build_application to return a mock PTB Application."""

    def _builder(self: Any, cfg: BotConfig) -> MagicMock:
        app = MagicMock()
        app.initialize = AsyncMock()
        app.start = AsyncMock()
        app.stop = AsyncMock()
        app.shutdown = AsyncMock()
        app.running = True
        app.updater = None
        app.bot = MagicMock()
        app.bot_data = {}
        return app

    return patch(
        "telegram_excerpt.manager.BotRegistry._build_application",
        _builder,
    )


# ─── BotRegistry ────────────────────────────────────────────────────


async def test_registry_reload_loads_enabled_bots() -> None:
    from telegram_excerpt.manager import BotRegistry

    cfg = _cfg(-100)
    disabled = _cfg(-200)
    disabled.enabled = False
    storage = _fake_storage([cfg, disabled])

    with _patch_build_app():
        registry = BotRegistry(storage)
        await registry.reload()

    assert registry.get(-100) is not None
    assert registry.get(-200) is None


async def test_registry_add_and_get() -> None:
    from telegram_excerpt.manager import BotRegistry

    storage = _fake_storage()
    registry = BotRegistry(storage)
    cfg = _cfg(-100)

    with _patch_build_app():
        await registry.add(cfg)

    entry = registry.get(-100)
    assert entry is not None
    got_cfg, _got_app = entry
    assert got_cfg.chat_id == -100


async def test_registry_add_duplicate_raises() -> None:
    from telegram_excerpt.manager import BotRegistry

    storage = _fake_storage()
    registry = BotRegistry(storage)
    cfg = _cfg(-100)

    with _patch_build_app():
        await registry.add(cfg)
        with pytest.raises(BotAlreadyRegisteredError):
            await registry.add(cfg)


async def test_registry_remove() -> None:
    from telegram_excerpt.manager import BotRegistry

    storage = _fake_storage()
    registry = BotRegistry(storage)
    cfg = _cfg(-100)

    with _patch_build_app():
        await registry.add(cfg)
        await registry.remove(-100)

    assert registry.get(-100) is None


async def test_registry_remove_nonexistent_raises() -> None:
    from telegram_excerpt.manager import BotRegistry

    storage = _fake_storage()
    registry = BotRegistry(storage)

    with pytest.raises(BotNotFoundError):
        await registry.remove(-999)


async def test_registry_get_by_hash() -> None:
    from telegram_excerpt.manager import BotRegistry

    storage = _fake_storage()
    registry = BotRegistry(storage)
    cfg = _cfg(-100)

    with _patch_build_app():
        await registry.add(cfg)

    entry = registry.get_by_hash(cfg.token_hash)
    assert entry is not None
    assert entry[0].chat_id == -100

    assert registry.get_by_hash("nonexistent") is None


async def test_registry_all_configs() -> None:
    from telegram_excerpt.manager import BotRegistry

    storage = _fake_storage()
    registry = BotRegistry(storage)

    with _patch_build_app():
        await registry.add(_cfg(-100))
        await registry.add(_cfg(-200))

    configs = registry.all_configs()
    chat_ids = {c.chat_id for c in configs}
    assert chat_ids == {-100, -200}


async def test_registry_all_chat_ids() -> None:
    from telegram_excerpt.manager import BotRegistry

    storage = _fake_storage()
    registry = BotRegistry(storage)

    with _patch_build_app():
        await registry.add(_cfg(-100))

    assert registry.all_chat_ids() == [-100]


# ─── _buffer_message_handler ────────────────────────────────────────


async def test_buffer_handler_stores_message() -> None:
    from telegram_excerpt.manager import _buffer_message_handler

    storage = AsyncMock()
    context = MagicMock()
    context.bot_data = {"storage": storage, "chat_id": -100}

    user = MagicMock()
    user.full_name = "Mario"
    user.id = 42

    msg = MagicMock()
    msg.message_id = 7
    msg.chat.id = -100
    msg.text = "ciao"
    msg.from_user = user
    msg.date = datetime(2026, 4, 5, 10, 0, tzinfo=UTC)

    update = MagicMock()
    update.effective_message = msg

    await _buffer_message_handler(update, context)

    storage.append_message.assert_awaited_once()
    buffered: BufferedMessage = storage.append_message.call_args[0][0]
    assert buffered.message_id == 7
    assert buffered.user_name == "Mario"
    assert buffered.text == "ciao"


async def test_buffer_handler_ignores_wrong_chat() -> None:
    from telegram_excerpt.manager import _buffer_message_handler

    storage = AsyncMock()
    context = MagicMock()
    context.bot_data = {"storage": storage, "chat_id": -100}

    msg = MagicMock()
    msg.message_id = 1
    msg.chat.id = -999  # wrong chat
    msg.text = "hello"
    msg.from_user = MagicMock()

    update = MagicMock()
    update.effective_message = msg

    await _buffer_message_handler(update, context)

    storage.append_message.assert_not_called()


async def test_buffer_handler_ignores_no_text() -> None:
    from telegram_excerpt.manager import _buffer_message_handler

    storage = AsyncMock()
    context = MagicMock()
    context.bot_data = {"storage": storage, "chat_id": -100}

    update = MagicMock()
    update.effective_message = None

    await _buffer_message_handler(update, context)

    storage.append_message.assert_not_called()


# ─── validate_token_and_chat ────────────────────────────────────────


async def test_validate_token_and_chat_returns_title() -> None:
    from telegram_excerpt.manager import validate_token_and_chat

    fake_chat = MagicMock()
    fake_chat.title = "Dev Group"
    fake_chat.full_name = None

    fake_bot = MagicMock()
    fake_bot.get_chat = AsyncMock(return_value=fake_chat)
    fake_bot.__aenter__ = AsyncMock(return_value=fake_bot)
    fake_bot.__aexit__ = AsyncMock(return_value=False)

    with patch("telegram.Bot", return_value=fake_bot):
        title = await validate_token_and_chat("tok:123", -100)

    assert title == "Dev Group"


# ─── make_bot_config ─────────────────────────────────────────────────


def test_make_bot_config_computes_hash() -> None:
    from telegram_excerpt.manager import make_bot_config

    cfg = make_bot_config(token="tok:ABC", chat_id=-100, chat_title="G", n=10)
    assert cfg.token_hash == compute_token_hash("tok:ABC")
    assert cfg.chat_title == "G"
    assert cfg.n == 10
