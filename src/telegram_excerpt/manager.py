"""Bot registry + orchestration of multiple ``telegram.ext.Application``.

For each registered bot we keep:

* the configuration persisted on Firestore (:class:`BotConfig`),
* a :class:`telegram.ext.Application` with a ``MessageHandler`` that
  buffers every received message.

The manager handles the lifecycle of the Applications in both runtime
modes:

* **polling** — ``start()`` launches the Updater of every bot as an
  asyncio task. ``stop()`` shuts them down.
* **webhook** — the Updaters are not started; the web layer manually
  forwards each update to ``application.process_update()``.

The class is thread-safe only with respect to asyncio (async lock).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from telegram import Update
from telegram.error import InvalidToken, TelegramError
from telegram.ext import Application, ApplicationBuilder, MessageHandler, filters

from telegram_excerpt.config import Mode, get_settings
from telegram_excerpt.exceptions import (
    BotAlreadyRegisteredError,
    BotNotFoundError,
    InvalidChatError,
    InvalidTokenError,
)
from telegram_excerpt.logging_conf import get_logger
from telegram_excerpt.models import BotConfig, BufferedMessage, compute_token_hash

if TYPE_CHECKING:
    from telegram.ext import ContextTypes

    from telegram_excerpt.storage import FirestoreStorage

log = get_logger(__name__)


class BotRegistry:
    """In-memory cache of registered bots and their PTB Applications.

    Args:
        storage: :class:`FirestoreStorage` instance for persistence.
    """

    def __init__(self, storage: FirestoreStorage) -> None:
        self._storage = storage
        self._apps: dict[int, Application] = {}  # key: chat_id
        self._configs: dict[int, BotConfig] = {}  # key: chat_id
        self._by_hash: dict[str, int] = {}  # token_hash → chat_id
        self._lock = asyncio.Lock()
        self._started = False

    # ─── Lifecycle ───────────────────────────────────────────────────
    async def reload(self) -> None:
        """Load all enabled bots from Firestore and build their Applications."""
        bots = await self._storage.load_bots()
        async with self._lock:
            for cfg in bots:
                if not cfg.enabled or cfg.chat_id in self._apps:
                    continue
                app = self._build_application(cfg)
                self._apps[cfg.chat_id] = app
                self._configs[cfg.chat_id] = cfg
                self._by_hash[cfg.token_hash] = cfg.chat_id
        log.info("manager.reloaded", count=len(self._configs))

    async def start(self) -> None:
        """Initialize every Application. Start polling if MODE=polling."""
        settings = get_settings()
        async with self._lock:
            for app in self._apps.values():
                await app.initialize()
                await app.start()
                if settings.mode is Mode.POLLING and app.updater is not None:
                    await app.updater.start_polling(
                        allowed_updates=Update.ALL_TYPES,
                        drop_pending_updates=False,
                    )
            self._started = True
        log.info("manager.started", mode=settings.mode.value, n_bots=len(self._apps))

    async def stop(self) -> None:
        """Gracefully shut down every Application."""
        async with self._lock:
            for chat_id, app in list(self._apps.items()):
                await self._shutdown_app(app, chat_id)
            self._started = False
        log.info("manager.stopped")

    # ─── Add / remove ────────────────────────────────────────────────
    async def add(self, cfg: BotConfig) -> None:
        """Add a new bot to the registry and start its Application.

        Raises:
            BotAlreadyRegisteredError: if chat_id is already registered.
        """
        async with self._lock:
            if cfg.chat_id in self._apps:
                raise BotAlreadyRegisteredError(f"bot for chat {cfg.chat_id} already registered")
            app = self._build_application(cfg)
            await app.initialize()
            await app.start()
            if get_settings().mode is Mode.POLLING and app.updater is not None:
                await app.updater.start_polling(
                    allowed_updates=Update.ALL_TYPES,
                    drop_pending_updates=False,
                )
            self._apps[cfg.chat_id] = app
            self._configs[cfg.chat_id] = cfg
            self._by_hash[cfg.token_hash] = cfg.chat_id
        log.info("manager.bot.added", bot_chat_id=cfg.chat_id)

    async def remove(self, chat_id: int) -> None:
        """Shut down and remove the bot from the in-memory registry.

        Raises:
            BotNotFoundError: if chat_id is not present.
        """
        async with self._lock:
            app = self._apps.pop(chat_id, None)
            cfg = self._configs.pop(chat_id, None)
            if cfg is not None:
                self._by_hash.pop(cfg.token_hash, None)
            if app is None or cfg is None:
                raise BotNotFoundError(f"bot {chat_id} not in registry")
            await self._shutdown_app(app, chat_id)
        log.info("manager.bot.removed", bot_chat_id=chat_id)

    # ─── Lookup ──────────────────────────────────────────────────────
    def get(self, chat_id: int) -> tuple[BotConfig, Application] | None:
        cfg = self._configs.get(chat_id)
        app = self._apps.get(chat_id)
        if cfg is None or app is None:
            return None
        return cfg, app

    def get_by_hash(self, token_hash: str) -> tuple[BotConfig, Application] | None:
        chat_id = self._by_hash.get(token_hash)
        if chat_id is None:
            return None
        return self.get(chat_id)

    def all_chat_ids(self) -> list[int]:
        return list(self._apps.keys())

    def all_configs(self) -> list[BotConfig]:
        return list(self._configs.values())

    # ─── Internals ───────────────────────────────────────────────────
    def _build_application(self, cfg: BotConfig) -> Application:
        """Build a PTB Application for a child bot.

        In webhook mode the Updater is not needed: updates arrive via
        HTTP POST and are processed manually through ``process_update``.
        """
        settings = get_settings()
        builder = ApplicationBuilder().token(cfg.token)
        if settings.mode is Mode.WEBHOOK:
            builder = builder.updater(None)
        app = builder.build()
        # Inject storage + chat_id into bot_data for the handler to use.
        app.bot_data["storage"] = self._storage
        app.bot_data["chat_id"] = cfg.chat_id
        # Group 0: buffer every text message.
        app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, _buffer_message_handler),
            group=0,
        )
        # Group 1: optional chat responder — runs in parallel to buffering.
        if settings.chat_responder_enabled:
            from telegram_excerpt.responder import (
                build_responder_client,
                responder_handler,
            )

            app.bot_data["responder_client"] = build_responder_client()
            app.add_handler(
                MessageHandler(filters.TEXT & ~filters.COMMAND, responder_handler),
                group=1,
            )
        return app

    @staticmethod
    async def _shutdown_app(app: Application, chat_id: int) -> None:
        try:
            if app.updater is not None and app.updater.running:
                await app.updater.stop()
            if app.running:
                await app.stop()
            await app.shutdown()
        except Exception as exc:
            log.warning("manager.shutdown.failed", bot_chat_id=chat_id, error=str(exc))


# ─── Handler ──────────────────────────────────────────────────────────


async def _buffer_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Generic handler for child bots: buffer every text message."""
    msg = update.effective_message
    if msg is None or msg.text is None:
        return
    storage: FirestoreStorage = context.bot_data["storage"]
    registered_chat_id: int = context.bot_data["chat_id"]
    # Defense: the bot should only be in registered_chat_id, but if it
    # were mistakenly added to other groups we ignore their messages.
    if msg.chat.id != registered_chat_id:
        log.warning(
            "manager.handler.unexpected_chat",
            expected=registered_chat_id,
            got=msg.chat.id,
        )
        return
    user = msg.from_user
    user_name = user.full_name if user else "Sconosciuto"
    buffered = BufferedMessage(
        message_id=msg.message_id,
        chat_id=registered_chat_id,
        user_id=user.id if user else None,
        user_name=user_name,
        text=msg.text,
        ts=(msg.date or datetime.now(UTC)).astimezone(UTC),
    )
    await storage.append_message(buffered)
    log.info(
        "manager.message.buffered",
        bot_chat_id=registered_chat_id,
        message_id=msg.message_id,
    )


# ─── Admin validation helpers ─────────────────────────────────────────


async def validate_token_and_chat(token: str, chat_id: int) -> str:
    """Verify that ``token`` has access to ``chat_id``.

    Creates a temporary ``Bot``, calls ``get_chat`` and returns the title.

    Raises:
        InvalidTokenError: Invalid token.
        InvalidChatError: The bot cannot access the group.
    """
    from telegram import Bot  # local import to avoid cycles

    try:
        bot = Bot(token)
        async with bot:
            chat = await bot.get_chat(chat_id)
    except InvalidToken as exc:
        raise InvalidTokenError(f"invalid telegram token: {exc}") from exc
    except TelegramError as exc:
        raise InvalidChatError(f"bot cannot access chat {chat_id}: {exc}") from exc
    return chat.title or chat.full_name or str(chat_id)


def make_bot_config(
    token: str,
    chat_id: int,
    chat_title: str,
    n: int,
) -> BotConfig:
    """Factory for ``BotConfig`` with derived fields populated."""
    return BotConfig(
        token=token,
        token_hash=compute_token_hash(token),
        chat_id=chat_id,
        chat_title=chat_title,
        n=n,
    )
