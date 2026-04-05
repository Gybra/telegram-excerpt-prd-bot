"""Flush processor: turns buffered messages into PRDs sent to the admin.

Flow:

1. fetch up to ``N`` messages from the bot's buffer (with id > last_read).
2. ``llm.classify_batch`` decides whether PRDs are needed.
3. If yes, ``llm.generate_prds`` produces one or more :class:`PRDDoc`.
4. Each PRD is sent to the ``FORWARD_CHAT_ID`` as a ``.md`` file with
   caption (author, timestamp, group).
5. Update ``last_read_message_id`` and delete the processed documents.

The 4 → 5 ordering guarantees that a send failure does not lose
messages: they are re-processed at the next tick. If the send succeeds
but a crash happens before ``clear_buffer``, at the next tick the filter
``message_id > last_read_message_id`` avoids double sending.
"""

from __future__ import annotations

import asyncio
import io
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from telegram import InputFile
from telegram.error import Forbidden, TelegramError

from telegram_excerpt.config import get_settings
from telegram_excerpt.exceptions import LLMError, StorageError
from telegram_excerpt.llm import classify_batch, generate_prds
from telegram_excerpt.logging_conf import get_logger

if TYPE_CHECKING:
    from telegram_excerpt.manager import BotRegistry
    from telegram_excerpt.models import BotConfig, PRDDoc
    from telegram_excerpt.storage import FirestoreStorage

log = get_logger(__name__)


class Processor:
    """Coordinates fetch buffer → classify → generate → send → cleanup.

    Args:
        storage: Firestore access.
        registry: registry to access the Application/child bot.
    """

    def __init__(
        self,
        storage: FirestoreStorage,
        registry: BotRegistry,
    ) -> None:
        self._storage = storage
        self._registry = registry
        self._tick_lock = asyncio.Lock()

    async def tick(self) -> dict[str, int]:
        """Process every silent bot. Returns aggregated stats.

        Guarded by an asyncio lock: if a previous tick is still running
        (e.g. Cloud Scheduler retry after a slow LLM call), the overlapping
        invocation returns immediately with ``skipped=1`` to avoid
        double-classification and double-send.
        """
        if self._tick_lock.locked():
            log.info("processor.tick.skipped_concurrent")
            return {"processed": 0, "prds_sent": 0, "skipped": 1}
        async with self._tick_lock:
            return await self._tick_inner()

    async def _tick_inner(self) -> dict[str, int]:
        settings = get_settings()
        threshold = datetime.now(UTC) - timedelta(seconds=settings.batch_silence_seconds)
        try:
            silent = await self._storage.list_silent_bots(threshold)
        except StorageError as exc:
            log.error("processor.list_silent.failed", error=str(exc))
            return {"processed": 0, "prds_sent": 0}

        processed = 0
        prds_sent = 0
        for bot_cfg in silent:
            try:
                sent = await self.flush_if_silent(bot_cfg)
            except Exception as exc:
                log.exception(
                    "processor.flush.failed",
                    bot_chat_id=bot_cfg.chat_id,
                    error=str(exc),
                )
                continue
            processed += 1
            prds_sent += sent
        log.info("processor.tick.done", processed=processed, prds_sent=prds_sent)
        return {"processed": processed, "prds_sent": prds_sent}

    async def flush_if_silent(self, bot_cfg: BotConfig) -> int:
        """Process the batch of a single bot if it has pending buffer.

        Returns:
            Number of PRDs sent.
        """
        messages = await self._storage.fetch_buffer(
            bot_cfg.chat_id,
            limit=bot_cfg.n,
            after_message_id=bot_cfg.last_read_message_id,
        )
        if not messages:
            # Nothing to do — reset the flag.
            await self._storage.update_bot(bot_cfg.chat_id, {"has_pending": False})
            log.debug("processor.flush.no_messages", bot_chat_id=bot_cfg.chat_id)
            return 0

        log.info(
            "processor.flush.start",
            bot_chat_id=bot_cfg.chat_id,
            n_messages=len(messages),
        )

        # 1. Classify
        try:
            verdict = await classify_batch(messages)
        except LLMError as exc:
            log.error(
                "processor.classify.failed",
                bot_chat_id=bot_cfg.chat_id,
                error=str(exc),
            )
            # Don't advance last_read: we'll retry at the next tick.
            return 0

        last_msg_id = messages[-1].message_id

        if not verdict.needs_prd:
            log.info(
                "processor.skip",
                bot_chat_id=bot_cfg.chat_id,
                reason=verdict.reason,
            )
            # Advance last_read anyway: avoid re-classifying the same
            # batch forever.
            await self._storage.set_last_read(bot_cfg.chat_id, last_msg_id)
            await self._storage.clear_buffer_up_to(bot_cfg.chat_id, last_msg_id)
            return 0

        # 2. Generate
        try:
            prds = await generate_prds(messages, chat_title=bot_cfg.chat_title)
        except LLMError as exc:
            log.error(
                "processor.generate.failed",
                bot_chat_id=bot_cfg.chat_id,
                error=str(exc),
            )
            return 0

        if not prds:
            log.warning("processor.generate.empty", bot_chat_id=bot_cfg.chat_id)
            await self._storage.set_last_read(bot_cfg.chat_id, last_msg_id)
            await self._storage.clear_buffer_up_to(bot_cfg.chat_id, last_msg_id)
            return 0

        # 3. Send
        sent = 0
        for prd in prds:
            try:
                await self._send_prd(prd, bot_cfg)
                sent += 1
            except TelegramError as exc:
                log.error(
                    "processor.send.failed",
                    bot_chat_id=bot_cfg.chat_id,
                    title=prd.title,
                    error=str(exc),
                )
            await asyncio.sleep(0.1)  # soft rate-limit

        # If every single send failed (Telegram down, 403 from child bot,
        # ...) do NOT advance last_read: the batch will be retried on the
        # next tick, otherwise all PRDs would be silently lost.
        if sent == 0 and prds:
            log.error(
                "processor.send.all_failed",
                bot_chat_id=bot_cfg.chat_id,
                n_prds=len(prds),
            )
            return 0

        # 4. Advance + cleanup
        await self._storage.set_last_read(bot_cfg.chat_id, last_msg_id)
        await self._storage.clear_buffer_up_to(bot_cfg.chat_id, last_msg_id)
        log.info(
            "processor.flush.done",
            bot_chat_id=bot_cfg.chat_id,
            prds_sent=sent,
            last_read_message_id=last_msg_id,
        )
        return sent

    async def _send_prd(self, prd: PRDDoc, bot_cfg: BotConfig) -> None:
        settings = get_settings()
        # PRDs are sent by the child bot attached to the source group,
        # not by the admin bot. The admin must have clicked /start on
        # that child bot in private chat at least once — otherwise the
        # Telegram API returns 403 Forbidden.
        entry = self._registry.get(bot_cfg.chat_id)
        if entry is None:
            log.error("processor.send.no_bot_in_registry", bot_chat_id=bot_cfg.chat_id)
            raise RuntimeError(f"bot {bot_cfg.chat_id} missing from registry during send")
        _, app = entry

        buf = io.BytesIO(prd.markdown.encode("utf-8"))
        buf.seek(0)
        document = InputFile(buf, filename=prd.filename())
        ts_str = prd.trigger_ts.astimezone().strftime("%Y-%m-%d %H:%M") if prd.trigger_ts else "N/A"
        caption = (
            f"📨 PRD — {prd.title}\n"
            f"👤 Autore: {prd.trigger_user}\n"
            f"🕐 {ts_str}\n"
            f"💬 Gruppo: {bot_cfg.chat_title or bot_cfg.chat_id}"
        )
        # Telegram limits caption to 1024 chars.
        if len(caption) > 1020:
            caption = caption[:1020] + "…"
        try:
            await app.bot.send_document(
                chat_id=settings.forward_chat_id,
                document=document,
                caption=caption,
            )
        except Forbidden:
            log.error(
                "processor.send.forbidden",
                bot_chat_id=bot_cfg.chat_id,
                hint=(
                    "admin must click /start on this child bot in private "
                    "chat before it can deliver PRDs"
                ),
            )
            raise
