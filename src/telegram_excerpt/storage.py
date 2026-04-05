"""Firestore async persistence layer.

Provides a typed, async interface over the system data:

* ``bots/{chat_id}`` — configuration of each registered child bot.
* ``bots/{chat_id}/buffer/{message_id}`` — messages not yet processed.

All operations are idempotent where sensible and do structured logging.
Firestore exceptions are re-raised as ``StorageError`` to isolate callers
from GCP-specific API.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from google.cloud import firestore  # type: ignore[attr-defined]
from google.cloud.firestore_v1 import AsyncClient
from google.cloud.firestore_v1.async_collection import AsyncCollectionReference
from google.cloud.firestore_v1.async_document import AsyncDocumentReference
from google.cloud.firestore_v1.base_query import FieldFilter

from telegram_excerpt.exceptions import BotNotFoundError, StorageError
from telegram_excerpt.logging_conf import get_logger
from telegram_excerpt.models import BotConfig, BufferedMessage

if TYPE_CHECKING:
    from collections.abc import Iterable

log = get_logger(__name__)

_BOTS_COLLECTION = "bots"
_BUFFER_SUBCOLLECTION = "buffer"


class FirestoreStorage:
    """Async wrapper around ``google.cloud.firestore.AsyncClient``.

    Args:
        project_id: GCP project ID.
        client: Injectable for tests (default: new AsyncClient).
    """

    def __init__(self, project_id: str, client: AsyncClient | None = None) -> None:
        self._project_id = project_id
        self._client: AsyncClient = client or AsyncClient(project=project_id)

    async def close(self) -> None:
        """Close the Firestore client (to be called at shutdown)."""
        await self._client.close()

    # ─── Bot registry ────────────────────────────────────────────────
    def _bot_doc(self, chat_id: int) -> AsyncDocumentReference:
        return self._client.collection(_BOTS_COLLECTION).document(str(chat_id))

    def _buffer_coll(self, chat_id: int) -> AsyncCollectionReference:
        return self._bot_doc(chat_id).collection(_BUFFER_SUBCOLLECTION)

    async def load_bots(self) -> list[BotConfig]:
        """Load all bots (including disabled ones) from the registry."""
        try:
            snapshots = self._client.collection(_BOTS_COLLECTION).stream()
            bots: list[BotConfig] = []
            async for snap in snapshots:
                data = snap.to_dict()
                if data is None:
                    continue
                try:
                    bots.append(BotConfig.from_firestore(data))
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "storage.bot.malformed", chat_id=snap.id, error=str(exc)
                    )
            log.info("storage.bots.loaded", count=len(bots))
            return bots
        except Exception as exc:  # noqa: BLE001
            raise StorageError(f"load_bots failed: {exc}") from exc

    async def get_bot(self, chat_id: int) -> BotConfig | None:
        try:
            snap = await self._bot_doc(chat_id).get()
            if not snap.exists:
                return None
            return BotConfig.from_firestore(snap.to_dict() or {})
        except Exception as exc:  # noqa: BLE001
            raise StorageError(f"get_bot({chat_id}) failed: {exc}") from exc

    async def add_bot(self, cfg: BotConfig) -> None:
        """Create the bot document; fail-fast if it already exists."""
        doc = self._bot_doc(cfg.chat_id)
        try:
            existing = await doc.get()
            if existing.exists:
                raise StorageError(f"bot {cfg.chat_id} already exists")
            await doc.set(cfg.to_firestore())
            log.info(
                "storage.bot.added",
                bot_chat_id=cfg.chat_id,
                token_hash=cfg.token_hash[:8],
            )
        except StorageError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise StorageError(f"add_bot({cfg.chat_id}) failed: {exc}") from exc

    async def remove_bot(self, chat_id: int) -> None:
        """Delete bot + buffer sub-collection."""
        doc = self._bot_doc(chat_id)
        try:
            # Delete buffer in batches first.
            await self._delete_buffer_subcollection(chat_id)
            await doc.delete()
            log.info("storage.bot.removed", bot_chat_id=chat_id)
        except Exception as exc:  # noqa: BLE001
            raise StorageError(f"remove_bot({chat_id}) failed: {exc}") from exc

    async def update_bot(self, chat_id: int, updates: dict[str, Any]) -> None:
        """Partial update of the bot document."""
        if not updates:
            return
        doc = self._bot_doc(chat_id)
        try:
            await doc.update(updates)
        except Exception as exc:  # noqa: BLE001
            raise StorageError(f"update_bot({chat_id}) failed: {exc}") from exc

    async def set_last_read(self, chat_id: int, message_id: int) -> None:
        """Update ``last_read_message_id`` and reset ``has_pending``."""
        await self.update_bot(
            chat_id,
            {"last_read_message_id": message_id, "has_pending": False},
        )

    async def list_silent_bots(self, threshold: datetime) -> list[BotConfig]:
        """Bots with ``last_message_ts <= threshold`` and pending buffer.

        Uses the ``has_pending`` flag to avoid N+1 queries on buffers.
        Requires a composite index (enabled, has_pending, last_message_ts) —
        Firestore suggests it on first error via a console link.
        """
        try:
            query = (
                self._client.collection(_BOTS_COLLECTION)
                .where(filter=FieldFilter("enabled", "==", True))
                .where(filter=FieldFilter("has_pending", "==", True))
                .where(filter=FieldFilter("last_message_ts", "<=", threshold))
            )
            bots: list[BotConfig] = []
            async for snap in query.stream():
                data = snap.to_dict()
                if data is not None:
                    bots.append(BotConfig.from_firestore(data))
            return bots
        except Exception as exc:  # noqa: BLE001
            raise StorageError(f"list_silent_bots failed: {exc}") from exc

    # ─── Message buffer ──────────────────────────────────────────────
    async def append_message(self, msg: BufferedMessage) -> None:
        """Append a message to the bot's buffer and set ``has_pending``.

        Uses a batch to atomically update the bot document
        (last_message_ts + has_pending) together with the insertion of
        the buffered message.
        """
        try:
            bot_doc = self._bot_doc(msg.chat_id)
            buffer_doc = self._buffer_coll(msg.chat_id).document(str(msg.message_id))
            batch = self._client.batch()
            batch.set(buffer_doc, msg.to_firestore())
            batch.update(
                bot_doc,
                {
                    "last_message_ts": msg.ts,
                    "has_pending": True,
                },
            )
            await batch.commit()
        except Exception as exc:  # noqa: BLE001
            raise StorageError(
                f"append_message(chat={msg.chat_id}, msg={msg.message_id}) failed: {exc}"
            ) from exc

    async def fetch_buffer(
        self, chat_id: int, *, limit: int, after_message_id: int = 0
    ) -> list[BufferedMessage]:
        """Return buffered messages ordered by ``message_id`` ascending.

        Args:
            chat_id: Group ID.
            limit: Max number of messages to return.
            after_message_id: Filter ``message_id > after_message_id``.
        """
        try:
            query = (
                self._buffer_coll(chat_id)
                .where(filter=FieldFilter("message_id", ">", after_message_id))
                .order_by("message_id", direction=firestore.Query.ASCENDING)
                .limit(limit)
            )
            out: list[BufferedMessage] = []
            async for snap in query.stream():
                data = snap.to_dict()
                if data is not None:
                    out.append(BufferedMessage.from_firestore(data))
            return out
        except Exception as exc:  # noqa: BLE001
            raise StorageError(f"fetch_buffer({chat_id}) failed: {exc}") from exc

    async def clear_buffer_up_to(self, chat_id: int, message_id: int) -> int:
        """Delete buffer documents with ``message_id <= message_id``.

        Returns:
            Number of documents deleted.
        """
        try:
            query = self._buffer_coll(chat_id).where(
                filter=FieldFilter("message_id", "<=", message_id)
            )
            to_delete: list[AsyncDocumentReference] = []
            async for snap in query.stream():
                to_delete.append(snap.reference)
            if not to_delete:
                return 0
            # Batch of max 500 operations.
            for chunk in _chunks(to_delete, 400):
                batch = self._client.batch()
                for ref in chunk:
                    batch.delete(ref)
                await batch.commit()
            log.info(
                "storage.buffer.cleared",
                bot_chat_id=chat_id,
                count=len(to_delete),
            )
            return len(to_delete)
        except Exception as exc:  # noqa: BLE001
            raise StorageError(
                f"clear_buffer_up_to({chat_id}, {message_id}) failed: {exc}"
            ) from exc

    async def require_bot(self, chat_id: int) -> BotConfig:
        """Like ``get_bot`` but raises ``BotNotFoundError`` if absent."""
        cfg = await self.get_bot(chat_id)
        if cfg is None:
            raise BotNotFoundError(f"bot {chat_id} not found")
        return cfg

    # ─── Internals ───────────────────────────────────────────────────
    async def _delete_buffer_subcollection(self, chat_id: int) -> None:
        refs: list[AsyncDocumentReference] = []
        async for snap in self._buffer_coll(chat_id).stream():
            refs.append(snap.reference)
        for chunk in _chunks(refs, 400):
            batch = self._client.batch()
            for ref in chunk:
                batch.delete(ref)
            await batch.commit()


def _chunks(seq: list[AsyncDocumentReference], size: int) -> Iterable[list[AsyncDocumentReference]]:
    for i in range(0, len(seq), size):
        yield seq[i : i + size]
