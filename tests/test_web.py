"""FastAPI endpoint tests (webhook mode)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from telegram_excerpt.models import compute_token_hash

# ─── Helpers ─────────────────────────────────────────────────────────


def _build_test_app() -> FastAPI:
    """Build a FastAPI app with pre-wired state (no real lifespan)."""
    from telegram_excerpt.web import _register_routes

    app = FastAPI()
    _register_routes(app)

    # Wire minimal state — storage mock needs a _client that supports
    # async iteration on .collection().limit().stream() for the healthcheck.
    storage = MagicMock()

    async def _empty_stream() -> AsyncIterator[None]:  # type: ignore[type-arg]
        return
        yield  # makes it an async generator

    storage._client.collection.return_value.limit.return_value.stream = _empty_stream
    app.state.storage = storage
    app.state.processor = MagicMock()
    app.state.processor.tick = AsyncMock(return_value={"processed": 1, "prds_sent": 0})

    app.state.admin_token_hash = compute_token_hash("123:ADMIN")
    app.state.admin_webhook_secret = "admin-secret-xyz"

    admin_app = MagicMock()
    admin_app.process_update = AsyncMock()
    admin_app.bot = MagicMock()
    app.state.admin_app = admin_app

    registry = MagicMock()
    registry.get_by_hash.return_value = None
    registry.all_configs.return_value = []
    app.state.registry = registry

    return app


@pytest.fixture
async def client() -> AsyncClient:
    app = _build_test_app()
    transport = ASGITransport(app=app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c  # type: ignore[misc]


# ─── /health ─────────────────────────────────────────────────────────


async def test_health(client: AsyncClient) -> None:
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


# ─── /webhook — auth ────────────────────────────────────────────────


async def test_webhook_unknown_hash(client: AsyncClient) -> None:
    resp = await client.post(
        "/webhook/badhash",
        json={"update_id": 1},
        headers={"X-Telegram-Bot-Api-Secret-Token": "x"},
    )
    assert resp.status_code == 404


async def test_webhook_admin_missing_secret(client: AsyncClient) -> None:
    admin_hash = compute_token_hash("123:ADMIN")
    resp = await client.post(f"/webhook/{admin_hash}", json={"update_id": 1})
    assert resp.status_code == 401


async def test_webhook_admin_wrong_secret(client: AsyncClient) -> None:
    admin_hash = compute_token_hash("123:ADMIN")
    resp = await client.post(
        f"/webhook/{admin_hash}",
        json={"update_id": 1},
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
    )
    assert resp.status_code == 401


async def test_webhook_admin_ok(client: AsyncClient) -> None:
    admin_hash = compute_token_hash("123:ADMIN")
    with patch("telegram_excerpt.web.Update") as mock_update_cls:
        fake_update = MagicMock()
        mock_update_cls.de_json.return_value = fake_update

        resp = await client.post(
            f"/webhook/{admin_hash}",
            json={"update_id": 1},
            headers={"X-Telegram-Bot-Api-Secret-Token": "admin-secret-xyz"},
        )

    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_webhook_child_bot_valid(client: AsyncClient) -> None:
    """Child bot webhook with correct secret → 200."""
    app = _build_test_app()
    child_hash = compute_token_hash("456:CHILD")
    cfg = MagicMock()
    cfg.webhook_secret = "child-secret"
    child_ptb = MagicMock()
    child_ptb.process_update = AsyncMock()
    child_ptb.bot = MagicMock()
    app.state.registry.get_by_hash.return_value = (cfg, child_ptb)

    transport = ASGITransport(app=app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        with patch("telegram_excerpt.web.Update") as mock_update_cls:
            mock_update_cls.de_json.return_value = MagicMock()
            resp = await c.post(
                f"/webhook/{child_hash}",
                json={"update_id": 2},
                headers={"X-Telegram-Bot-Api-Secret-Token": "child-secret"},
            )

    assert resp.status_code == 200


async def test_webhook_child_bot_wrong_secret(client: AsyncClient) -> None:
    app = _build_test_app()
    child_hash = compute_token_hash("456:CHILD")
    cfg = MagicMock()
    cfg.webhook_secret = "child-secret"
    child_ptb = MagicMock()
    app.state.registry.get_by_hash.return_value = (cfg, child_ptb)

    transport = ASGITransport(app=app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.post(
            f"/webhook/{child_hash}",
            json={"update_id": 2},
            headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
        )

    assert resp.status_code == 401


# ─── /tasks/process — bearer auth ───────────────────────────────────


async def test_tasks_process_no_bearer(client: AsyncClient) -> None:
    resp = await client.post("/tasks/process")
    assert resp.status_code == 401


async def test_tasks_process_wrong_bearer(client: AsyncClient) -> None:
    resp = await client.post(
        "/tasks/process",
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert resp.status_code == 401


async def test_tasks_process_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """Valid bearer → tick() called, result returned."""
    monkeypatch.setenv("MODE", "webhook")
    monkeypatch.setenv("BASE_URL", "https://example.run.app")
    monkeypatch.setenv("SCHEDULER_AUTH_TOKEN", "s" * 32)
    from telegram_excerpt.config import get_settings

    get_settings.cache_clear()

    app = _build_test_app()
    transport = ASGITransport(app=app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.post(
            "/tasks/process",
            headers={"Authorization": f"Bearer {'s' * 32}"},
        )
    assert resp.status_code == 200
    assert "processed" in resp.json()


# ─── /admin/setup — bearer auth ─────────────────────────────────────


async def test_admin_setup_no_bearer(client: AsyncClient) -> None:
    resp = await client.post("/admin/setup")
    assert resp.status_code == 401
