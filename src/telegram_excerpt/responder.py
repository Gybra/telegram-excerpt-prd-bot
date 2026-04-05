"""Chat responder: optional LLM reply to every group message.

Opt-in via ``CHAT_RESPONDER_ENABLED=true``. When enabled, every child bot
registers an additional handler that, for each incoming text message in
its group, calls the LLM and posts a reply in the same thread.

The system prompt instructs the model to return the literal string
``SKIP`` when it has nothing valuable to say — this keeps the bot from
polluting the chat with filler responses.
"""

from __future__ import annotations

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


def build_responder_client() -> AsyncOpenAI:
    """Build the OpenRouter client for the chat responder."""
    settings = get_settings()
    return AsyncOpenAI(
        base_url=settings.openrouter_base_url,
        api_key=settings.openrouter_api_key.get_secret_value(),
    )


async def responder_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
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
        log.warning(
            "responder.reply.failed", message_id=msg.message_id, error=str(exc)
        )
