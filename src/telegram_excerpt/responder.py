"""Chat responder: optional LLM reply to every group message.

Opt-in via ``CHAT_RESPONDER_ENABLED=true``. When enabled, every child bot
registers an additional handler that, for each incoming text message in
its group, calls the LLM and posts a reply in the same thread.

The system prompt instructs the model to return the literal string
``SKIP`` when it has nothing valuable to say — this keeps the bot from
polluting the chat with filler responses.

Guardrails:

* **Per-user rate-limit** — at most ``CHAT_RESPONDER_RATE_LIMIT`` replies
  per user within a sliding window of ``CHAT_RESPONDER_RATE_WINDOW_SECONDS``.
* **Daily budget** — at most ``CHAT_RESPONDER_DAILY_BUDGET`` LLM calls
  per calendar day (UTC). Set to 0 to disable the cap.
"""

from __future__ import annotations

import time
from collections import deque
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from openai import APIError, AsyncOpenAI
from telegram import Update
from telegram.error import TelegramError

from telegram_excerpt.config import get_settings
from telegram_excerpt.logging_conf import get_logger

if TYPE_CHECKING:
    from telegram.ext import ContextTypes

log = get_logger(__name__)

_SKIP_TOKEN = "SKIP"

_responder_client: AsyncOpenAI | None = None


def build_responder_client() -> AsyncOpenAI:
    """Return a process-wide AsyncOpenAI client for the chat responder.

    Cached at module level so that N child bots share a single HTTP
    connection pool instead of opening N independent ones.
    """
    global _responder_client  # noqa: PLW0603
    if _responder_client is None:
        settings = get_settings()
        _responder_client = AsyncOpenAI(
            base_url=settings.openrouter_base_url,
            api_key=settings.openrouter_api_key.get_secret_value(),
        )
    return _responder_client


# ─── Rate-limit + daily budget ───────────────────────────────────────

# Per-user sliding window: user_id → deque of timestamps
_user_calls: dict[int, deque[float]] = {}

# Daily budget counter: (utc_date_str, count)
_daily_counter: list[str | int] = ["", 0]


def _check_rate_limit(user_id: int) -> bool:
    """Return True if the user is within the rate limit."""
    settings = get_settings()
    now = time.monotonic()
    window = settings.chat_responder_rate_window_seconds
    limit = settings.chat_responder_rate_limit

    timestamps = _user_calls.get(user_id)
    if timestamps is None:
        timestamps = deque()
        _user_calls[user_id] = timestamps

    # Evict expired entries
    cutoff = now - window
    while timestamps and timestamps[0] < cutoff:
        timestamps.popleft()

    if len(timestamps) >= limit:
        return False

    timestamps.append(now)
    return True


def _check_daily_budget() -> bool:
    """Return True if the daily budget has not been exhausted."""
    settings = get_settings()
    budget = settings.chat_responder_daily_budget
    if budget == 0:
        return True  # unlimited

    today = datetime.now(UTC).strftime("%Y-%m-%d")
    if _daily_counter[0] != today:
        _daily_counter[0] = today
        _daily_counter[1] = 0

    count = int(_daily_counter[1])
    if count >= budget:
        return False

    _daily_counter[1] = count + 1
    return True


async def responder_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reply to a group message via the LLM (if the model doesn't skip).

    Reads the AsyncOpenAI client from ``context.bot_data["responder_client"]``
    (injected by the manager). On any error it logs and silently skips,
    so a responder failure never blocks the buffering pipeline.
    """
    msg = update.effective_message
    if msg is None or not msg.text:
        return

    # Skip commands and messages sent by bots.
    if msg.text.startswith("/"):
        return
    if msg.from_user is not None and msg.from_user.is_bot:
        return

    settings = get_settings()
    client: AsyncOpenAI | None = context.bot_data.get("responder_client")
    if client is None:
        return

    # Guardrails
    user_id = msg.from_user.id if msg.from_user else 0
    if not _check_rate_limit(user_id):
        log.debug("responder.rate_limited", user_id=user_id)
        return
    if not _check_daily_budget():
        log.warning("responder.daily_budget_exhausted")
        return

    model = settings.chat_responder_model or settings.openrouter_model

    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": settings.chat_responder_system_prompt},
                {"role": "user", "content": msg.text},
            ],
            max_tokens=settings.chat_responder_max_tokens,
            temperature=0.6,
        )
    except APIError as exc:
        log.warning("responder.llm.failed", error=str(exc))
        return

    if not response.choices:
        return
    reply = (response.choices[0].message.content or "").strip()
    if not reply or reply.upper() == _SKIP_TOKEN or reply.upper().startswith(_SKIP_TOKEN):
        log.debug("responder.skip", message_id=msg.message_id)
        return

    try:
        await msg.reply_text(reply)
    except TelegramError as exc:
        log.warning("responder.reply.failed", message_id=msg.message_id, error=str(exc))
