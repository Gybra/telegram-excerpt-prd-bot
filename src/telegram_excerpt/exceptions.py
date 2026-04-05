"""Domain exceptions for the telegram-excerpt system.

Every custom exception inherits from ``TelegramExcerptError`` so that
callers can catch them selectively, without accidentally masking
generic runtime errors (``ValueError``, ``KeyError``, etc.).
"""

from __future__ import annotations


class TelegramExcerptError(Exception):
    """Project base exception."""


# ─── Registry / manager ───────────────────────────────────────────────
class BotAlreadyRegisteredError(TelegramExcerptError):
    """A bot for this chat_id is already present in the registry."""


class BotNotFoundError(TelegramExcerptError):
    """Requested bot not found in the registry."""


class InvalidTokenError(TelegramExcerptError):
    """Telegram token is invalid or has no access to the requested group."""


class InvalidChatError(TelegramExcerptError):
    """Chat ID is unreachable with the provided token."""


# ─── Webhook / auth ───────────────────────────────────────────────────
class WebhookAuthError(TelegramExcerptError):
    """Webhook secret token missing or incorrect."""


class SchedulerAuthError(TelegramExcerptError):
    """Bearer token for internal endpoints is incorrect."""


# ─── LLM ──────────────────────────────────────────────────────────────
class LLMError(TelegramExcerptError):
    """Generic LLM integration error."""


class LLMClassificationError(LLMError):
    """The model did not return a parsable classification."""


class LLMGenerationError(LLMError):
    """The model did not return parsable PRDs."""


# ─── Storage ──────────────────────────────────────────────────────────
class StorageError(TelegramExcerptError):
    """Generic Firestore persistence error."""
