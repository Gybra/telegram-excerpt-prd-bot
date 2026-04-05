"""Flush processor tests (with storage, LLM and registry mocked)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.error import TelegramError

from telegram_excerpt.models import BotConfig, BufferedMessage, ClassifyResult, PRDDoc
from telegram_excerpt.processor import Processor


class FakeStorage:
    """In-memory storage used only by tests."""

    def __init__(self) -> None:
        self.buffer: dict[int, list[BufferedMessage]] = {}
        self.last_read: dict[int, int] = {}
        self.updates: list[tuple[int, dict[str, Any]]] = []
        self.cleared: list[tuple[int, int]] = []

    async def fetch_buffer(
        self, chat_id: int, *, limit: int, after_message_id: int
    ) -> list[BufferedMessage]:
        msgs = self.buffer.get(chat_id, [])
        return [m for m in msgs if m.message_id > after_message_id][:limit]

    async def set_last_read(self, chat_id: int, message_id: int) -> None:
        self.last_read[chat_id] = message_id

    async def clear_buffer_up_to(self, chat_id: int, message_id: int) -> int:
        before = len(self.buffer.get(chat_id, []))
        self.buffer[chat_id] = [
            m for m in self.buffer.get(chat_id, []) if m.message_id > message_id
        ]
        removed = before - len(self.buffer[chat_id])
        self.cleared.append((chat_id, message_id))
        return removed

    async def update_bot(self, chat_id: int, updates: dict[str, Any]) -> None:
        self.updates.append((chat_id, updates))


class FakeRegistry:
    """Minimal registry stub: returns a mock Application per chat_id."""

    def __init__(self) -> None:
        self.bot = AsyncMock()
        self.bot.send_document = AsyncMock()
        self._app = MagicMock()
        self._app.bot = self.bot

    def get(self, chat_id: int) -> tuple[Any, Any]:
        return (None, self._app)


@pytest.fixture
def fake_storage() -> FakeStorage:
    return FakeStorage()


@pytest.fixture
def fake_registry() -> FakeRegistry:
    return FakeRegistry()


@pytest.fixture
def bot_cfg_factory() -> Callable[..., BotConfig]:
    from telegram_excerpt.models import compute_token_hash

    def _make(chat_id: int = -100, n: int = 10, last_read: int = 0) -> BotConfig:
        tok = f"{abs(chat_id)}:FAKE"
        return BotConfig(
            token=tok,
            token_hash=compute_token_hash(tok),
            chat_id=chat_id,
            chat_title="Test",
            n=n,
            last_read_message_id=last_read,
        )

    return _make


def _msg(i: int, chat_id: int = -100) -> BufferedMessage:
    return BufferedMessage(
        message_id=i,
        chat_id=chat_id,
        user_id=1,
        user_name="Mario",
        text=f"msg {i}",
        ts=datetime(2026, 4, 5, 10, i, tzinfo=UTC),
    )


async def test_flush_no_messages_resets_flag(
    fake_storage: FakeStorage,
    fake_registry: FakeRegistry,
    bot_cfg_factory: Callable[..., BotConfig],
) -> None:
    proc = Processor(storage=fake_storage, registry=fake_registry)  # type: ignore[arg-type]
    cfg = bot_cfg_factory()
    sent = await proc.flush_if_silent(cfg)
    assert sent == 0
    assert fake_storage.updates == [(cfg.chat_id, {"has_pending": False})]


async def test_flush_classify_false_advances_last_read(
    fake_storage: FakeStorage,
    fake_registry: FakeRegistry,
    bot_cfg_factory: Callable[..., BotConfig],
) -> None:
    fake_storage.buffer[-100] = [_msg(1), _msg(2)]
    proc = Processor(storage=fake_storage, registry=fake_registry)  # type: ignore[arg-type]
    cfg = bot_cfg_factory()

    with patch(
        "telegram_excerpt.processor.classify_batch",
        new=AsyncMock(return_value=ClassifyResult(needs_prd=False, reason="chit")),
    ):
        sent = await proc.flush_if_silent(cfg)

    assert sent == 0
    assert fake_storage.last_read[-100] == 2
    assert (-100, 2) in fake_storage.cleared
    fake_registry.bot.send_document.assert_not_called()


async def test_flush_classify_true_sends_prds(
    fake_storage: FakeStorage,
    fake_registry: FakeRegistry,
    bot_cfg_factory: Callable[..., BotConfig],
) -> None:
    fake_storage.buffer[-100] = [_msg(1), _msg(2)]
    proc = Processor(storage=fake_storage, registry=fake_registry)  # type: ignore[arg-type]
    cfg = bot_cfg_factory()

    prds = [
        PRDDoc(
            title="A",
            markdown="body",
            trigger_message_id=1,
            trigger_user="Mario",
            trigger_ts=datetime(2026, 4, 5, 10, 1, tzinfo=UTC),
        ),
        PRDDoc(
            title="B",
            markdown="body2",
            trigger_message_id=2,
            trigger_user="Luigi",
            trigger_ts=datetime(2026, 4, 5, 10, 2, tzinfo=UTC),
        ),
    ]
    with (
        patch(
            "telegram_excerpt.processor.classify_batch",
            new=AsyncMock(return_value=ClassifyResult(needs_prd=True)),
        ),
        patch(
            "telegram_excerpt.processor.generate_prds",
            new=AsyncMock(return_value=prds),
        ),
    ):
        sent = await proc.flush_if_silent(cfg)

    assert sent == 2
    assert fake_registry.bot.send_document.await_count == 2
    # caption contains author and group
    call = fake_registry.bot.send_document.await_args_list[0]
    caption = call.kwargs["caption"]
    assert "Mario" in caption
    assert "Test" in caption
    # last_read advanced, buffer cleaned
    assert fake_storage.last_read[-100] == 2
    assert (-100, 2) in fake_storage.cleared


async def test_flush_empty_prds_still_advances(
    fake_storage: FakeStorage,
    fake_registry: FakeRegistry,
    bot_cfg_factory: Callable[..., BotConfig],
) -> None:
    fake_storage.buffer[-100] = [_msg(5)]
    proc = Processor(storage=fake_storage, registry=fake_registry)  # type: ignore[arg-type]
    cfg = bot_cfg_factory()
    with (
        patch(
            "telegram_excerpt.processor.classify_batch",
            new=AsyncMock(return_value=ClassifyResult(needs_prd=True)),
        ),
        patch(
            "telegram_excerpt.processor.generate_prds",
            new=AsyncMock(return_value=[]),
        ),
    ):
        sent = await proc.flush_if_silent(cfg)
    assert sent == 0
    assert fake_storage.last_read[-100] == 5


async def test_flush_all_sends_fail_preserves_batch(
    fake_storage: FakeStorage,
    fake_registry: FakeRegistry,
    bot_cfg_factory: Callable[..., BotConfig],
) -> None:
    """If every send_document raises, last_read must NOT advance."""
    fake_storage.buffer[-100] = [_msg(1), _msg(2)]
    cfg = bot_cfg_factory()
    proc = Processor(storage=fake_storage, registry=fake_registry)  # type: ignore[arg-type]
    fake_registry.bot.send_document.side_effect = TelegramError("boom")

    prds = [
        PRDDoc(
            title="A",
            markdown="body",
            trigger_message_id=1,
            trigger_user="Mario",
            trigger_ts=datetime(2026, 4, 5, 10, 1, tzinfo=UTC),
        ),
    ]
    with (
        patch(
            "telegram_excerpt.processor.classify_batch",
            new=AsyncMock(return_value=ClassifyResult(needs_prd=True)),
        ),
        patch(
            "telegram_excerpt.processor.generate_prds",
            new=AsyncMock(return_value=prds),
        ),
    ):
        sent = await proc.flush_if_silent(cfg)

    assert sent == 0
    # last_read NOT advanced → retry at next tick
    assert -100 not in fake_storage.last_read
    assert fake_storage.cleared == []


async def test_tick_lock_skips_concurrent_invocation(
    fake_storage: FakeStorage,
    fake_registry: FakeRegistry,
) -> None:
    """A second tick() while the first is running returns skipped=1."""
    proc = Processor(storage=fake_storage, registry=fake_registry)  # type: ignore[arg-type]

    # Block the inner tick on an event so we can overlap two calls.
    gate = asyncio.Event()

    async def _blocking_inner() -> dict[str, int]:
        await gate.wait()
        return {"processed": 0, "prds_sent": 0}

    with patch.object(proc, "_tick_inner", new=_blocking_inner):
        first = asyncio.create_task(proc.tick())
        # Give the first call time to acquire the lock.
        await asyncio.sleep(0)
        second = await proc.tick()
        assert second.get("skipped") == 1
        gate.set()
        result = await first
        assert "skipped" not in result


async def test_flush_respects_last_read(
    fake_storage: FakeStorage,
    fake_registry: FakeRegistry,
    bot_cfg_factory: Callable[..., BotConfig],
) -> None:
    fake_storage.buffer[-100] = [_msg(1), _msg(2), _msg(3)]
    cfg = bot_cfg_factory(last_read=2)
    proc = Processor(storage=fake_storage, registry=fake_registry)  # type: ignore[arg-type]
    captured: list[BufferedMessage] = []

    async def _classify(msgs: list[BufferedMessage]) -> ClassifyResult:
        captured.extend(msgs)
        return ClassifyResult(needs_prd=False)

    with patch("telegram_excerpt.processor.classify_batch", new=_classify):
        await proc.flush_if_silent(cfg)

    assert [m.message_id for m in captured] == [3]
