"""Admin bot command handler tests."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from telegram import Chat, Message, Update, User

from telegram_excerpt.exceptions import (
    BotAlreadyRegisteredError,
    BotNotFoundError,
    InvalidChatError,
    InvalidTokenError,
    StorageError,
)
from telegram_excerpt.models import BotConfig, compute_token_hash

# ─── Helpers ─────────────────────────────────────────────────────────


def _make_update(
    chat_id: int = 999,
    text: str = "/help",
    user_id: int = 1,
) -> Update:
    """Build a minimal Update with effective_chat, effective_message, from_user."""
    user = User(id=user_id, is_bot=False, first_name="Admin")
    chat = Chat(id=chat_id, type=Chat.PRIVATE)
    msg = MagicMock(spec=Message)
    msg.text = text
    msg.chat = chat
    msg.from_user = user
    msg.reply_text = AsyncMock()

    update = MagicMock(spec=Update)
    update.effective_chat = chat
    update.effective_message = msg
    update.effective_user = user
    return update


def _make_context(
    args: list[str] | None = None,
    storage: Any = None,
    registry: Any = None,
) -> MagicMock:
    ctx = MagicMock()
    ctx.args = args or []
    ctx.application.bot_data = {
        "storage": storage or AsyncMock(),
        "registry": registry or MagicMock(),
    }
    return ctx


def _bot_cfg(chat_id: int = -100, n: int = 10) -> BotConfig:
    tok = f"{abs(chat_id)}:FAKE"
    return BotConfig(
        token=tok,
        token_hash=compute_token_hash(tok),
        chat_id=chat_id,
        chat_title="Test",
        n=n,
    )


# ─── Auth guard ──────────────────────────────────────────────────────


async def test_unauthorized_user_is_rejected() -> None:
    from telegram_excerpt.admin import _cmd_help

    update = _make_update(chat_id=666)  # not 999
    ctx = _make_context()
    await _cmd_help(update, ctx)
    update.effective_message.reply_text.assert_not_called()


async def test_authorized_user_gets_help() -> None:
    from telegram_excerpt.admin import _cmd_help

    update = _make_update(chat_id=999)
    ctx = _make_context()
    await _cmd_help(update, ctx)
    update.effective_message.reply_text.assert_awaited_once()
    text = update.effective_message.reply_text.call_args[0][0]
    assert "/add_bot" in text
    assert "/remove_bot" in text


# ─── /start delegates to /help ───────────────────────────────────────


async def test_start_shows_help() -> None:
    from telegram_excerpt.admin import _cmd_start

    update = _make_update()
    ctx = _make_context()
    await _cmd_start(update, ctx)
    update.effective_message.reply_text.assert_awaited_once()
    text = update.effective_message.reply_text.call_args[0][0]
    assert "/help" in text


# ─── /add_bot ────────────────────────────────────────────────────────


async def test_add_bot_missing_args() -> None:
    from telegram_excerpt.admin import _cmd_add_bot

    update = _make_update()
    ctx = _make_context(args=["only-token"])
    await _cmd_add_bot(update, ctx)
    reply = update.effective_message.reply_text.call_args[0][0]
    assert "Uso:" in reply


async def test_add_bot_invalid_chat_id() -> None:
    from telegram_excerpt.admin import _cmd_add_bot

    update = _make_update()
    ctx = _make_context(args=["tok:123", "not_int"])
    await _cmd_add_bot(update, ctx)
    reply = update.effective_message.reply_text.call_args[0][0]
    assert "intero" in reply


async def test_add_bot_n_out_of_range() -> None:
    from telegram_excerpt.admin import _cmd_add_bot

    update = _make_update()
    ctx = _make_context(args=["tok:123", "-100", "999"])
    await _cmd_add_bot(update, ctx)
    reply = update.effective_message.reply_text.call_args[0][0]
    assert "range" in reply


async def test_add_bot_invalid_token() -> None:
    from telegram_excerpt.admin import _cmd_add_bot

    update = _make_update()
    storage = AsyncMock()
    registry = MagicMock()
    ctx = _make_context(args=["tok:BAD", "-100"], storage=storage, registry=registry)

    with patch(
        "telegram_excerpt.admin.validate_token_and_chat",
        new=AsyncMock(side_effect=InvalidTokenError("bad")),
    ):
        await _cmd_add_bot(update, ctx)

    reply = update.effective_message.reply_text.call_args[0][0]
    assert "Token" in reply


async def test_add_bot_invalid_chat() -> None:
    from telegram_excerpt.admin import _cmd_add_bot

    update = _make_update()
    ctx = _make_context(args=["tok:OK", "-100"])

    with patch(
        "telegram_excerpt.admin.validate_token_and_chat",
        new=AsyncMock(side_effect=InvalidChatError("no access")),
    ):
        await _cmd_add_bot(update, ctx)

    reply = update.effective_message.reply_text.call_args[0][0]
    assert "accesso" in reply


async def test_add_bot_storage_error() -> None:
    from telegram_excerpt.admin import _cmd_add_bot

    update = _make_update()
    storage = AsyncMock()
    storage.add_bot = AsyncMock(side_effect=StorageError("boom"))
    registry = MagicMock()
    ctx = _make_context(args=["tok:OK", "-100"], storage=storage, registry=registry)

    with patch(
        "telegram_excerpt.admin.validate_token_and_chat",
        new=AsyncMock(return_value="TestGroup"),
    ):
        await _cmd_add_bot(update, ctx)

    reply = update.effective_message.reply_text.call_args[0][0]
    assert "persistenza" in reply


async def test_add_bot_already_registered() -> None:
    from telegram_excerpt.admin import _cmd_add_bot

    update = _make_update()
    storage = AsyncMock()
    registry = MagicMock()
    registry.add = AsyncMock(side_effect=BotAlreadyRegisteredError("dup"))
    ctx = _make_context(args=["tok:OK", "-100"], storage=storage, registry=registry)

    with patch(
        "telegram_excerpt.admin.validate_token_and_chat",
        new=AsyncMock(return_value="TestGroup"),
    ):
        await _cmd_add_bot(update, ctx)

    reply = update.effective_message.reply_text.call_args[0][0]
    assert "già registrato" in reply


async def test_add_bot_success_polling() -> None:
    from telegram_excerpt.admin import _cmd_add_bot

    update = _make_update()
    storage = AsyncMock()
    registry = MagicMock()
    registry.add = AsyncMock()
    ctx = _make_context(args=["tok:OK", "-100", "20"], storage=storage, registry=registry)

    with patch(
        "telegram_excerpt.admin.validate_token_and_chat",
        new=AsyncMock(return_value="TestGroup"),
    ):
        await _cmd_add_bot(update, ctx)

    reply = update.effective_message.reply_text.call_args[0][0]
    assert "Registrato" in reply or "✅" in reply
    storage.add_bot.assert_awaited_once()
    registry.add.assert_awaited_once()


# ─── /remove_bot ─────────────────────────────────────────────────────


async def test_remove_bot_missing_args() -> None:
    from telegram_excerpt.admin import _cmd_remove_bot

    update = _make_update()
    ctx = _make_context(args=[])
    await _cmd_remove_bot(update, ctx)
    reply = update.effective_message.reply_text.call_args[0][0]
    assert "Uso:" in reply


async def test_remove_bot_success() -> None:
    from telegram_excerpt.admin import _cmd_remove_bot

    update = _make_update()
    storage = AsyncMock()
    registry = MagicMock()
    registry.get.return_value = None  # no webhook cleanup needed
    registry.remove = AsyncMock()
    ctx = _make_context(args=["-100"], storage=storage, registry=registry)

    await _cmd_remove_bot(update, ctx)

    reply = update.effective_message.reply_text.call_args[0][0]
    assert "✅" in reply
    storage.remove_bot.assert_awaited_once()


async def test_remove_bot_storage_error() -> None:
    from telegram_excerpt.admin import _cmd_remove_bot

    update = _make_update()
    storage = AsyncMock()
    storage.remove_bot = AsyncMock(side_effect=StorageError("boom"))
    registry = MagicMock()
    registry.get.return_value = None
    registry.remove = AsyncMock(side_effect=BotNotFoundError("nope"))
    ctx = _make_context(args=["-100"], storage=storage, registry=registry)

    await _cmd_remove_bot(update, ctx)

    reply = update.effective_message.reply_text.call_args[0][0]
    assert "persistenza" in reply


# ─── /list_bots ──────────────────────────────────────────────────────


async def test_list_bots_empty() -> None:
    from telegram_excerpt.admin import _cmd_list_bots

    update = _make_update()
    registry = MagicMock()
    registry.all_configs.return_value = []
    ctx = _make_context(registry=registry)

    await _cmd_list_bots(update, ctx)

    reply = update.effective_message.reply_text.call_args[0][0]
    assert "Nessun bot" in reply


async def test_list_bots_shows_entries() -> None:
    from telegram_excerpt.admin import _cmd_list_bots

    update = _make_update()
    registry = MagicMock()
    cfg = _bot_cfg(chat_id=-100)
    registry.all_configs.return_value = [cfg]
    ctx = _make_context(registry=registry)

    await _cmd_list_bots(update, ctx)

    reply = update.effective_message.reply_text.call_args[0][0]
    assert "Test" in reply
    assert "-100" in reply


# ─── /set_n ──────────────────────────────────────────────────────────


async def test_set_n_missing_args() -> None:
    from telegram_excerpt.admin import _cmd_set_n

    update = _make_update()
    ctx = _make_context(args=["-100"])
    await _cmd_set_n(update, ctx)
    reply = update.effective_message.reply_text.call_args[0][0]
    assert "Uso:" in reply


async def test_set_n_out_of_range() -> None:
    from telegram_excerpt.admin import _cmd_set_n

    update = _make_update()
    ctx = _make_context(args=["-100", "0"])
    await _cmd_set_n(update, ctx)
    reply = update.effective_message.reply_text.call_args[0][0]
    assert "range" in reply


async def test_set_n_bot_not_found() -> None:
    from telegram_excerpt.admin import _cmd_set_n

    update = _make_update()
    registry = MagicMock()
    registry.get.return_value = None
    ctx = _make_context(args=["-100", "25"], registry=registry)

    await _cmd_set_n(update, ctx)

    reply = update.effective_message.reply_text.call_args[0][0]
    assert "non trovato" in reply


async def test_set_n_success() -> None:
    from telegram_excerpt.admin import _cmd_set_n

    update = _make_update()
    storage = AsyncMock()
    registry = MagicMock()
    cfg = _bot_cfg(chat_id=-100, n=10)
    app_mock = MagicMock()
    registry.get.return_value = (cfg, app_mock)
    ctx = _make_context(args=["-100", "25"], storage=storage, registry=registry)

    await _cmd_set_n(update, ctx)

    reply = update.effective_message.reply_text.call_args[0][0]
    assert "✅" in reply
    assert "25" in reply
    storage.update_bot.assert_awaited_once_with(-100, {"n": 25})
    assert cfg.n == 25
