"""LLM prompt + parser tests (with mocked OpenAI client)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from telegram_excerpt.exceptions import LLMClassificationError, LLMGenerationError
from telegram_excerpt.llm import classify_batch, generate_prds
from telegram_excerpt.models import BufferedMessage


def _fake_client(content: str) -> Any:
    """Build an AsyncOpenAI mock that returns ``content``."""
    client = MagicMock()
    response = MagicMock()
    choice = MagicMock()
    choice.message.content = content
    response.choices = [choice]
    client.chat.completions.create = AsyncMock(return_value=response)
    return client


def _msgs() -> list[BufferedMessage]:
    return [
        BufferedMessage(
            message_id=1,
            chat_id=-100,
            user_id=1,
            user_name="Mario",
            text="C'è un bug nel login",
            ts=datetime(2026, 4, 5, 10, 0, tzinfo=UTC),
        ),
        BufferedMessage(
            message_id=2,
            chat_id=-100,
            user_id=2,
            user_name="Luigi",
            text="Confermo, vedo errore 500",
            ts=datetime(2026, 4, 5, 10, 1, tzinfo=UTC),
        ),
    ]


async def test_classify_batch_needs_prd_true() -> None:
    client = _fake_client(json.dumps({"needs_prd": True, "reason": "bug login"}))
    result = await classify_batch(_msgs(), client=client)
    assert result.needs_prd is True
    assert result.reason == "bug login"


async def test_classify_batch_needs_prd_false() -> None:
    client = _fake_client(json.dumps({"needs_prd": False, "reason": "chitchat"}))
    result = await classify_batch(_msgs(), client=client)
    assert result.needs_prd is False


async def test_classify_batch_empty_messages() -> None:
    client = _fake_client("{}")
    result = await classify_batch([], client=client)
    assert result.needs_prd is False
    # client should not have been called
    client.chat.completions.create.assert_not_called()


async def test_classify_batch_invalid_json() -> None:
    client = _fake_client("not json at all")
    with pytest.raises(LLMClassificationError):
        await classify_batch(_msgs(), client=client)


async def test_classify_batch_fenced_json() -> None:
    client = _fake_client('```json\n{"needs_prd": true, "reason": "ok"}\n```')
    result = await classify_batch(_msgs(), client=client)
    assert result.needs_prd is True


async def test_classify_batch_missing_key() -> None:
    client = _fake_client(json.dumps({"reason": "x"}))
    with pytest.raises(LLMClassificationError):
        await classify_batch(_msgs(), client=client)


async def test_generate_prds_happy_path() -> None:
    payload = {
        "prds": [
            {
                "title": "Bug login",
                "trigger_message_id": 1,
                "markdown": "# PRD: Bug login\n\n**Autore:** Mario\n...",
            }
        ]
    }
    client = _fake_client(json.dumps(payload))
    prds = await generate_prds(_msgs(), chat_title="Dev", client=client)
    assert len(prds) == 1
    assert prds[0].title == "Bug login"
    assert prds[0].trigger_message_id == 1
    assert prds[0].trigger_user == "Mario"


async def test_generate_prds_fallback_trigger() -> None:
    # trigger_message_id not present in messages → fallback to last one
    payload = {
        "prds": [
            {
                "title": "X",
                "trigger_message_id": 999,
                "markdown": "body",
            }
        ]
    }
    client = _fake_client(json.dumps(payload))
    prds = await generate_prds(_msgs(), client=client)
    assert prds[0].trigger_message_id == 2  # last of the batch
    assert prds[0].trigger_user == "Luigi"


async def test_generate_prds_empty_list() -> None:
    client = _fake_client(json.dumps({"prds": []}))
    prds = await generate_prds(_msgs(), client=client)
    assert prds == []


async def test_generate_prds_missing_array() -> None:
    client = _fake_client(json.dumps({"foo": "bar"}))
    with pytest.raises(LLMGenerationError):
        await generate_prds(_msgs(), client=client)


async def test_generate_prds_skips_item_without_markdown() -> None:
    payload = {
        "prds": [
            {"title": "No body", "trigger_message_id": 1, "markdown": ""},
            {"title": "Has body", "trigger_message_id": 2, "markdown": "x"},
        ]
    }
    client = _fake_client(json.dumps(payload))
    prds = await generate_prds(_msgs(), client=client)
    assert len(prds) == 1
    assert prds[0].title == "Has body"
