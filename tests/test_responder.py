"""Chat responder handler tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from openai import APIError

from telegram_excerpt.responder import responder_handler

# ─── Helpers ─────────────────────────────────────────────────────────


def _make_update(
    text: str = "Ciao come va?",
    is_bot: bool = False,
    message_id: int = 1,
) -> MagicMock:
    user = MagicMock()
    user.is_bot = is_bot

    msg = MagicMock()
    msg.text = text
    msg.message_id = message_id
    msg.from_user = user
    msg.reply_text = AsyncMock()

    update = MagicMock()
    update.effective_message = msg
    return update


def _make_context(client: MagicMock | None = None) -> MagicMock:
    ctx = MagicMock()
    ctx.bot_data = {"responder_client": client}
    return ctx


def _fake_openai_client(content: str) -> MagicMock:
    client = MagicMock()
    response = MagicMock()
    choice = MagicMock()
    choice.message.content = content
    response.choices = [choice]
    client.chat.completions.create = AsyncMock(return_value=response)
    return client


# ─── Tests ───────────────────────────────────────────────────────────


async def test_responder_replies_with_llm_content() -> None:
    client = _fake_openai_client("Tutto bene, grazie!")
    update = _make_update()
    ctx = _make_context(client)

    await responder_handler(update, ctx)

    update.effective_message.reply_text.assert_awaited_once_with("Tutto bene, grazie!")


async def test_responder_skips_when_llm_returns_skip() -> None:
    client = _fake_openai_client("SKIP")
    update = _make_update()
    ctx = _make_context(client)

    await responder_handler(update, ctx)

    update.effective_message.reply_text.assert_not_called()


async def test_responder_skips_when_llm_returns_skip_prefix() -> None:
    client = _fake_openai_client("SKIP - nothing useful to say")
    update = _make_update()
    ctx = _make_context(client)

    await responder_handler(update, ctx)

    update.effective_message.reply_text.assert_not_called()


async def test_responder_skips_empty_reply() -> None:
    client = _fake_openai_client("   ")
    update = _make_update()
    ctx = _make_context(client)

    await responder_handler(update, ctx)

    update.effective_message.reply_text.assert_not_called()


async def test_responder_skips_no_choices() -> None:
    client = MagicMock()
    response = MagicMock()
    response.choices = []
    client.chat.completions.create = AsyncMock(return_value=response)

    update = _make_update()
    ctx = _make_context(client)

    await responder_handler(update, ctx)

    update.effective_message.reply_text.assert_not_called()


async def test_responder_ignores_commands() -> None:
    client = _fake_openai_client("should not be called")
    update = _make_update(text="/start")
    ctx = _make_context(client)

    await responder_handler(update, ctx)

    client.chat.completions.create.assert_not_called()
    update.effective_message.reply_text.assert_not_called()


async def test_responder_ignores_bot_messages() -> None:
    client = _fake_openai_client("should not be called")
    update = _make_update(is_bot=True)
    ctx = _make_context(client)

    await responder_handler(update, ctx)

    client.chat.completions.create.assert_not_called()


async def test_responder_ignores_no_message() -> None:
    update = MagicMock()
    update.effective_message = None
    ctx = _make_context()

    await responder_handler(update, ctx)  # should not raise


async def test_responder_ignores_no_client() -> None:
    update = _make_update()
    ctx = MagicMock()
    ctx.bot_data = {}  # no responder_client key

    await responder_handler(update, ctx)

    update.effective_message.reply_text.assert_not_called()


async def test_responder_handles_api_error_gracefully() -> None:
    client = MagicMock()
    client.chat.completions.create = AsyncMock(
        side_effect=APIError(
            message="rate limited",
            request=MagicMock(),
            body=None,
        )
    )
    update = _make_update()
    ctx = _make_context(client)

    await responder_handler(update, ctx)  # should not raise

    update.effective_message.reply_text.assert_not_called()


async def test_responder_handles_telegram_send_error() -> None:
    from telegram.error import TelegramError

    client = _fake_openai_client("A valid reply")
    update = _make_update()
    update.effective_message.reply_text = AsyncMock(side_effect=TelegramError("forbidden"))
    ctx = _make_context(client)

    await responder_handler(update, ctx)  # should not raise


# ─── Rate-limit ──────────────────────────────────────────────────────


async def test_responder_rate_limits_user(monkeypatch: MagicMock) -> None:
    """After RATE_LIMIT calls, subsequent calls from the same user are dropped."""
    import telegram_excerpt.responder as mod

    # Reset state
    mod._user_calls.clear()

    # Set rate limit to 2 calls per 60s window
    monkeypatch.setenv("CHAT_RESPONDER_RATE_LIMIT", "2")
    monkeypatch.setenv("CHAT_RESPONDER_RATE_WINDOW_SECONDS", "60")
    from telegram_excerpt.config import get_settings

    get_settings.cache_clear()

    client = _fake_openai_client("reply")
    ctx = _make_context(client)

    # First 2 calls should go through
    for i in range(2):
        update = _make_update(message_id=i + 1)
        update.effective_message.from_user.id = 42
        await responder_handler(update, ctx)

    assert client.chat.completions.create.call_count == 2

    # 3rd call should be rate-limited
    update = _make_update(message_id=3)
    update.effective_message.from_user.id = 42
    await responder_handler(update, ctx)

    assert client.chat.completions.create.call_count == 2  # no new call

    # Different user should still work
    update = _make_update(message_id=4)
    update.effective_message.from_user.id = 99
    await responder_handler(update, ctx)

    assert client.chat.completions.create.call_count == 3

    mod._user_calls.clear()


async def test_responder_daily_budget_exhausted(monkeypatch: MagicMock) -> None:
    """After daily budget is reached, all calls are dropped."""
    import telegram_excerpt.responder as mod

    mod._user_calls.clear()
    mod._daily_counter[0] = ""
    mod._daily_counter[1] = 0

    monkeypatch.setenv("CHAT_RESPONDER_DAILY_BUDGET", "1")
    monkeypatch.setenv("CHAT_RESPONDER_RATE_LIMIT", "100")
    from telegram_excerpt.config import get_settings

    get_settings.cache_clear()

    client = _fake_openai_client("reply")
    ctx = _make_context(client)

    # First call uses the budget
    update = _make_update(message_id=1)
    update.effective_message.from_user.id = 42
    await responder_handler(update, ctx)
    assert client.chat.completions.create.call_count == 1

    # Second call: budget exhausted
    update = _make_update(message_id=2)
    update.effective_message.from_user.id = 43
    await responder_handler(update, ctx)
    assert client.chat.completions.create.call_count == 1  # no new call

    mod._user_calls.clear()
    mod._daily_counter[0] = ""
    mod._daily_counter[1] = 0


# ─── build_responder_client ──────────────────────────────────────────


def test_build_responder_client_is_singleton() -> None:
    import telegram_excerpt.responder as mod

    # Reset module-level singleton
    mod._responder_client = None
    try:
        c1 = mod.build_responder_client()
        c2 = mod.build_responder_client()
        assert c1 is c2
    finally:
        mod._responder_client = None
