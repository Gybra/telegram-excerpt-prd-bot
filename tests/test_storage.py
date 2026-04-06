"""FirestoreStorage tests with mocked AsyncClient."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from telegram_excerpt.exceptions import BotNotFoundError, StorageError
from telegram_excerpt.models import BotConfig, BufferedMessage, compute_token_hash
from telegram_excerpt.storage import FirestoreStorage

# ─── Helpers ─────────────────────────────────────────────────────────


def _cfg(chat_id: int = -100) -> BotConfig:
    tok = f"{abs(chat_id)}:FAKE"
    return BotConfig(
        token=tok,
        token_hash=compute_token_hash(tok),
        chat_id=chat_id,
        chat_title="Test",
        n=10,
    )


def _msg(msg_id: int = 1, chat_id: int = -100) -> BufferedMessage:
    return BufferedMessage(
        message_id=msg_id,
        chat_id=chat_id,
        user_id=42,
        user_name="Mario",
        text=f"msg {msg_id}",
        ts=datetime(2026, 4, 5, 10, 0, tzinfo=UTC),
    )


def _fake_snap(data: dict[str, Any] | None, exists: bool = True) -> MagicMock:
    snap = MagicMock()
    snap.exists = exists
    snap.to_dict.return_value = data
    snap.id = str(data.get("chat_id", "0")) if data else "0"
    return snap


class AsyncIterHelper:
    """Make a list behave as an async iterator (for Firestore .stream())."""

    def __init__(self, items: list[Any]) -> None:
        self._items = items
        self._idx = 0

    def __aiter__(self) -> AsyncIterHelper:
        self._idx = 0
        return self

    async def __anext__(self) -> Any:
        if self._idx >= len(self._items):
            raise StopAsyncIteration
        item = self._items[self._idx]
        self._idx += 1
        return item


def _mock_client() -> MagicMock:
    """Build a MagicMock that mimics AsyncClient enough for our tests."""
    client = MagicMock()
    # batch support
    batch = MagicMock()
    batch.set = MagicMock()
    batch.update = MagicMock()
    batch.delete = MagicMock()
    batch.commit = AsyncMock()
    client.batch.return_value = batch
    return client


# ─── close ───────────────────────────────────────────────────────────


async def test_close_awaits_client() -> None:
    client = MagicMock()
    client.close = AsyncMock()
    storage = FirestoreStorage(project_id="test", client=client)
    await storage.close()
    client.close.assert_awaited_once()


# ─── get_bot ─────────────────────────────────────────────────────────


async def test_get_bot_found() -> None:
    cfg = _cfg(-100)
    snap = _fake_snap(cfg.to_firestore())
    client = _mock_client()
    client.collection.return_value.document.return_value.get = AsyncMock(return_value=snap)
    storage = FirestoreStorage(project_id="test", client=client)

    result = await storage.get_bot(-100)
    assert result is not None
    assert result.chat_id == -100


async def test_get_bot_not_found() -> None:
    snap = _fake_snap(None, exists=False)
    client = _mock_client()
    client.collection.return_value.document.return_value.get = AsyncMock(return_value=snap)
    storage = FirestoreStorage(project_id="test", client=client)

    result = await storage.get_bot(-100)
    assert result is None


async def test_get_bot_error_raises_storage_error() -> None:
    client = _mock_client()
    client.collection.return_value.document.return_value.get = AsyncMock(
        side_effect=RuntimeError("boom")
    )
    storage = FirestoreStorage(project_id="test", client=client)

    with pytest.raises(StorageError, match="get_bot"):
        await storage.get_bot(-100)


# ─── add_bot ─────────────────────────────────────────────────────────


async def test_add_bot_success() -> None:
    snap = _fake_snap(None, exists=False)
    client = _mock_client()
    doc = client.collection.return_value.document.return_value
    doc.get = AsyncMock(return_value=snap)
    doc.set = AsyncMock()
    storage = FirestoreStorage(project_id="test", client=client)

    cfg = _cfg(-100)
    await storage.add_bot(cfg)
    doc.set.assert_awaited_once()


async def test_add_bot_duplicate_raises() -> None:
    snap = _fake_snap({"chat_id": -100}, exists=True)
    client = _mock_client()
    doc = client.collection.return_value.document.return_value
    doc.get = AsyncMock(return_value=snap)
    storage = FirestoreStorage(project_id="test", client=client)

    with pytest.raises(StorageError, match="already exists"):
        await storage.add_bot(_cfg(-100))


# ─── remove_bot ──────────────────────────────────────────────────────


async def test_remove_bot_success() -> None:
    client = _mock_client()
    doc = client.collection.return_value.document.return_value
    doc.delete = AsyncMock()
    # buffer subcollection is empty
    doc.collection.return_value.stream.return_value = AsyncIterHelper([])
    storage = FirestoreStorage(project_id="test", client=client)

    await storage.remove_bot(-100)
    doc.delete.assert_awaited_once()


# ─── update_bot ──────────────────────────────────────────────────────


async def test_update_bot() -> None:
    client = _mock_client()
    doc = client.collection.return_value.document.return_value
    doc.update = AsyncMock()
    storage = FirestoreStorage(project_id="test", client=client)

    await storage.update_bot(-100, {"n": 25})
    doc.update.assert_awaited_once_with({"n": 25})


async def test_update_bot_empty_is_noop() -> None:
    client = _mock_client()
    doc = client.collection.return_value.document.return_value
    doc.update = AsyncMock()
    storage = FirestoreStorage(project_id="test", client=client)

    await storage.update_bot(-100, {})
    doc.update.assert_not_called()


# ─── set_last_read ───────────────────────────────────────────────────


async def test_set_last_read_updates_fields() -> None:
    client = _mock_client()
    doc = client.collection.return_value.document.return_value
    doc.update = AsyncMock()
    storage = FirestoreStorage(project_id="test", client=client)

    await storage.set_last_read(-100, 42)
    doc.update.assert_awaited_once_with({"last_read_message_id": 42, "has_pending": False})


# ─── require_bot ─────────────────────────────────────────────────────


async def test_require_bot_found() -> None:
    cfg = _cfg(-100)
    snap = _fake_snap(cfg.to_firestore())
    client = _mock_client()
    client.collection.return_value.document.return_value.get = AsyncMock(return_value=snap)
    storage = FirestoreStorage(project_id="test", client=client)

    result = await storage.require_bot(-100)
    assert result.chat_id == -100


async def test_require_bot_not_found_raises() -> None:
    snap = _fake_snap(None, exists=False)
    client = _mock_client()
    client.collection.return_value.document.return_value.get = AsyncMock(return_value=snap)
    storage = FirestoreStorage(project_id="test", client=client)

    with pytest.raises(BotNotFoundError):
        await storage.require_bot(-100)


# ─── load_bots ───────────────────────────────────────────────────────


async def test_load_bots_returns_valid_configs() -> None:
    cfg = _cfg(-100)
    snaps = [_fake_snap(cfg.to_firestore())]
    client = _mock_client()
    client.collection.return_value.stream.return_value = AsyncIterHelper(snaps)
    storage = FirestoreStorage(project_id="test", client=client)

    bots = await storage.load_bots()
    assert len(bots) == 1
    assert bots[0].chat_id == -100


async def test_load_bots_skips_malformed() -> None:
    good = _fake_snap(_cfg(-100).to_firestore())
    bad = _fake_snap({"garbage": True})
    client = _mock_client()
    client.collection.return_value.stream.return_value = AsyncIterHelper([good, bad])
    storage = FirestoreStorage(project_id="test", client=client)

    bots = await storage.load_bots()
    assert len(bots) == 1


# ─── append_message ──────────────────────────────────────────────────


async def test_append_message_uses_batch() -> None:
    client = _mock_client()
    doc = client.collection.return_value.document.return_value
    doc.collection.return_value.document.return_value = MagicMock()
    storage = FirestoreStorage(project_id="test", client=client)

    msg = _msg(1, -100)
    await storage.append_message(msg)

    batch = client.batch.return_value
    batch.set.assert_called_once()
    batch.update.assert_called_once()
    batch.commit.assert_awaited_once()
