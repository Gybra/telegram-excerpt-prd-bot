"""Unit tests for the domain models."""

from __future__ import annotations

from telegram_excerpt.models import (
    BotConfig,
    PRDDoc,
    compute_token_hash,
    generate_webhook_secret,
)


def test_compute_token_hash_is_stable() -> None:
    token = "123:ABC-DEF"
    h1 = compute_token_hash(token)
    h2 = compute_token_hash(token)
    assert h1 == h2
    assert len(h1) == 16
    assert all(c in "0123456789abcdef" for c in h1)


def test_compute_token_hash_changes_with_token() -> None:
    assert compute_token_hash("a") != compute_token_hash("b")


def test_generate_webhook_secret_uniqueness() -> None:
    secrets = {generate_webhook_secret() for _ in range(50)}
    assert len(secrets) == 50  # high collision-improbability
    assert all(len(s) >= 32 for s in secrets)


def test_bot_config_repr_does_not_leak_token() -> None:
    cfg = BotConfig(
        token="SECRET-TOKEN-123",
        token_hash="abcd" * 4,
        chat_id=-100,
        n=10,
    )
    assert "SECRET-TOKEN-123" not in repr(cfg)
    assert "SECRET-TOKEN-123" not in str(cfg)


def test_bot_config_redacted_token() -> None:
    cfg = BotConfig(token="x", token_hash="abcdef1234567890", chat_id=-100, n=10)
    assert "abcdef12" in cfg.redacted_token()
    assert "abcdef1234567890" not in cfg.redacted_token()


def test_prd_filename_sanitization() -> None:
    prd = PRDDoc(title="Fix: login bug / #42!", markdown="x")
    fname = prd.filename()
    assert fname.endswith(".md")
    assert fname.startswith("PRD_")
    # no slashes or illegal chars
    for bad in '/\\:*?"<>|':
        assert bad not in fname


def test_prd_filename_empty_title() -> None:
    prd = PRDDoc(title="", markdown="x")
    assert prd.filename() == "PRD_PRD.md"
