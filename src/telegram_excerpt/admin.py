"""Admin bot commands.

The admin bot is a dedicated :class:`telegram.ext.Application`, distinct
from the child bots. It exposes commands to dynamically manage the
registry:

* ``/add_bot <token> <chat_id> [N]`` — register a new child bot.
* ``/remove_bot <chat_id>`` — remove a child bot.
* ``/list_bots`` — list registered bots.
* ``/set_n <chat_id> <N>`` — update the batch size for a bot.
* ``/help`` — show command help.
* ``/start`` — alias of ``/help``.

Every command is only accessible from the chat_id declared in
``FORWARD_CHAT_ID``: other users are silently ignored to avoid leaking
the bot's existence.

Note: user-facing text (help, replies) is in Italian by project
convention — fork and adapt as needed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from telegram_excerpt.config import Mode, get_settings
from telegram_excerpt.exceptions import (
    BotAlreadyRegisteredError,
    BotNotFoundError,
    InvalidChatError,
    InvalidTokenError,
    StorageError,
)
from telegram_excerpt.logging_conf import get_logger
from telegram_excerpt.manager import (
    BotRegistry,
    make_bot_config,
    validate_token_and_chat,
)

if TYPE_CHECKING:
    from telegram_excerpt.storage import FirestoreStorage

log = get_logger(__name__)


HELP_TEXT = (
    "🤖 telegram-excerpt — comandi admin\n"
    "\n"
    "/add_bot <token> <chat_id> [N]\n"
    "  Registra un nuovo bot figlio sul gruppo chat_id.\n"
    "  N (opzionale) è il max messaggi per batch (default: DEFAULT_N).\n"
    "\n"
    "/remove_bot <chat_id>\n"
    "  Rimuove il bot associato al gruppo.\n"
    "\n"
    "/list_bots\n"
    "  Elenca tutti i bot registrati.\n"
    "\n"
    "/set_n <chat_id> <N>\n"
    "  Aggiorna il batch-size di un bot.\n"
    "\n"
    "/help\n"
    "  Mostra questo messaggio.\n"
    "\n"
    "Nota: prima di registrare un bot, disabilita la privacy mode via "
    "@BotFather (/setprivacy → Disable) e aggiungi il bot al gruppo target."
)


# ─── Bot data keys ────────────────────────────────────────────────────

_KEY_STORAGE = "storage"
_KEY_REGISTRY = "registry"


# ─── Auth guard ───────────────────────────────────────────────────────


def _is_authorized(update: Update) -> bool:
    """True if the update comes from the configured FORWARD_CHAT_ID."""
    chat = update.effective_chat
    if chat is None:
        return False
    return chat.id == get_settings().forward_chat_id


async def _reject(update: Update) -> None:
    """Log + silently ignore (don't reply to avoid leaking the bot)."""
    user = update.effective_user
    log.warning(
        "admin.unauthorized",
        user_id=user.id if user else None,
        chat_id=update.effective_chat.id if update.effective_chat else None,
    )


# ─── Handlers ─────────────────────────────────────────────────────────


async def _cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        await _reject(update)
        return
    await _cmd_help(update, ctx)


async def _cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:  # noqa: ARG001
    if not _is_authorized(update):
        await _reject(update)
        return
    assert update.effective_message is not None
    await update.effective_message.reply_text(HELP_TEXT)


async def _cmd_add_bot(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        await _reject(update)
        return
    assert update.effective_message is not None
    args = ctx.args or []
    if len(args) < 2:
        await update.effective_message.reply_text(
            "Uso: /add_bot <token> <chat_id> [N]"
        )
        return

    token = args[0].strip()
    try:
        chat_id = int(args[1])
    except ValueError:
        await update.effective_message.reply_text("chat_id deve essere un intero.")
        return
    n = get_settings().default_n
    if len(args) >= 3:
        try:
            n = int(args[2])
        except ValueError:
            await update.effective_message.reply_text("N deve essere un intero.")
            return
        if n < 1 or n > 500:
            await update.effective_message.reply_text("N fuori range (1-500).")
            return

    storage: FirestoreStorage = ctx.application.bot_data[_KEY_STORAGE]
    registry: BotRegistry = ctx.application.bot_data[_KEY_REGISTRY]

    # 1. Validate token + group access
    try:
        chat_title = await validate_token_and_chat(token, chat_id)
    except InvalidTokenError:
        await update.effective_message.reply_text("❌ Token Telegram non valido.")
        return
    except InvalidChatError as exc:
        await update.effective_message.reply_text(
            f"❌ Il bot non ha accesso al gruppo {chat_id}.\n"
            "Assicurati di:\n"
            "• Aver aggiunto il bot al gruppo\n"
            "• Aver disabilitato la privacy mode (@BotFather → /setprivacy)\n\n"
            f"Dettagli: {exc}"
        )
        return

    cfg = make_bot_config(token=token, chat_id=chat_id, chat_title=chat_title, n=n)

    # 2. Persist + register
    try:
        await storage.add_bot(cfg)
    except StorageError as exc:
        await update.effective_message.reply_text(f"❌ Errore persistenza: {exc}")
        return
    try:
        await registry.add(cfg)
    except BotAlreadyRegisteredError:
        await update.effective_message.reply_text("⚠️ Bot già registrato in memoria.")
        return

    # 3. Webhook mode: set the webhook on Telegram
    settings = get_settings()
    if settings.mode is Mode.WEBHOOK:
        webhook_url = f"{settings.base_url}/webhook/{cfg.token_hash}"
        try:
            _, app = registry.get(chat_id) or (None, None)
            if app is not None:
                await app.bot.set_webhook(
                    url=webhook_url,
                    secret_token=cfg.webhook_secret,
                    allowed_updates=Update.ALL_TYPES,
                )
        except Exception as exc:  # noqa: BLE001
            # Roll back: a bot without webhook is useless and would leave
            # the admin in a half-registered state impossible to re-add.
            log.warning(
                "admin.add_bot.webhook_failed_rollback",
                chat_id=chat_id,
                error=str(exc),
            )
            try:
                await registry.remove(chat_id)
            except BotNotFoundError:
                pass
            try:
                await storage.remove_bot(chat_id)
            except StorageError as rollback_exc:
                log.error(
                    "admin.add_bot.rollback_failed",
                    chat_id=chat_id,
                    error=str(rollback_exc),
                )
            await update.effective_message.reply_text(
                f"❌ setWebhook fallito, registrazione annullata: {exc}"
            )
            return

    await update.effective_message.reply_text(
        f"✅ Bot registrato su {chat_title}\n"
        f"chat_id: {chat_id}\n"
        f"N: {n}\n\n"
        "⚠️ Apri il bot figlio in chat privata e clicca /start una volta,"
        " altrimenti non può inviarti i PRD."
    )


async def _cmd_remove_bot(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        await _reject(update)
        return
    assert update.effective_message is not None
    args = ctx.args or []
    if len(args) < 1:
        await update.effective_message.reply_text("Uso: /remove_bot <chat_id>")
        return
    try:
        chat_id = int(args[0])
    except ValueError:
        await update.effective_message.reply_text("chat_id deve essere un intero.")
        return

    storage: FirestoreStorage = ctx.application.bot_data[_KEY_STORAGE]
    registry: BotRegistry = ctx.application.bot_data[_KEY_REGISTRY]

    # Webhook mode: first remove the webhook from Telegram
    settings = get_settings()
    entry = registry.get(chat_id)
    if settings.mode is Mode.WEBHOOK and entry is not None:
        _, app = entry
        try:
            await app.bot.delete_webhook()
        except Exception as exc:  # noqa: BLE001
            log.warning("admin.delete_webhook.failed", chat_id=chat_id, error=str(exc))

    try:
        await registry.remove(chat_id)
    except BotNotFoundError:
        pass  # continue to clean up Firestore even if not in memory

    try:
        await storage.remove_bot(chat_id)
    except StorageError as exc:
        await update.effective_message.reply_text(f"❌ Errore persistenza: {exc}")
        return

    await update.effective_message.reply_text(f"✅ Bot {chat_id} rimosso.")


async def _cmd_list_bots(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        await _reject(update)
        return
    assert update.effective_message is not None
    registry: BotRegistry = ctx.application.bot_data[_KEY_REGISTRY]
    configs = registry.all_configs()
    if not configs:
        await update.effective_message.reply_text("Nessun bot registrato.")
        return
    lines = ["Bot registrati:", ""]
    for cfg in configs:
        status = "🟢" if cfg.enabled else "🔴"
        pending = "📨" if cfg.has_pending else "—"
        title = cfg.chat_title or "(senza titolo)"
        lines.append(
            f"{status} {title}\n"
            f"  chat_id: {cfg.chat_id}  N: {cfg.n}  "
            f"pending: {pending}  last_read: {cfg.last_read_message_id}"
        )
    await update.effective_message.reply_text("\n".join(lines))


async def _cmd_set_n(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        await _reject(update)
        return
    assert update.effective_message is not None
    args = ctx.args or []
    if len(args) < 2:
        await update.effective_message.reply_text("Uso: /set_n <chat_id> <N>")
        return
    try:
        chat_id = int(args[0])
        n = int(args[1])
    except ValueError:
        await update.effective_message.reply_text("chat_id e N devono essere interi.")
        return
    if n < 1 or n > 500:
        await update.effective_message.reply_text("N fuori range (1-500).")
        return

    storage: FirestoreStorage = ctx.application.bot_data[_KEY_STORAGE]
    registry: BotRegistry = ctx.application.bot_data[_KEY_REGISTRY]

    entry = registry.get(chat_id)
    if entry is None:
        await update.effective_message.reply_text("Bot non trovato.")
        return
    cfg, _ = entry
    try:
        await storage.update_bot(chat_id, {"n": n})
    except StorageError as exc:
        await update.effective_message.reply_text(f"❌ Errore persistenza: {exc}")
        return
    cfg.n = n
    await update.effective_message.reply_text(
        f"✅ N aggiornato a {n} per chat {chat_id}."
    )


# ─── Wiring ───────────────────────────────────────────────────────────


def build_admin_application(
    storage: FirestoreStorage, registry: BotRegistry
) -> Application:
    """Build the admin bot :class:`Application` with registered handlers."""
    settings = get_settings()
    builder = ApplicationBuilder_factory(
        settings.telegram_admin_bot_token.get_secret_value()
    )
    if settings.mode is Mode.WEBHOOK:
        builder = builder.updater(None)
    app = builder.build()
    app.bot_data[_KEY_STORAGE] = storage
    app.bot_data[_KEY_REGISTRY] = registry
    app.add_handler(CommandHandler("start", _cmd_start))
    app.add_handler(CommandHandler("help", _cmd_help))
    app.add_handler(CommandHandler("add_bot", _cmd_add_bot))
    app.add_handler(CommandHandler("remove_bot", _cmd_remove_bot))
    app.add_handler(CommandHandler("list_bots", _cmd_list_bots))
    app.add_handler(CommandHandler("set_n", _cmd_set_n))
    return app


def ApplicationBuilder_factory(token: str):  # noqa: ANN201, N802
    """Isolated factory so it can be mocked in tests."""
    from telegram.ext import ApplicationBuilder

    return ApplicationBuilder().token(token)
