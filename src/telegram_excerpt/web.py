"""FastAPI app (webhook mode).

Exposed publicly on Cloud Run. Endpoints:

* ``POST /webhook/{token_hash}`` — receives Telegram updates for child
  bots or admin. Validated against ``X-Telegram-Bot-Api-Secret-Token``.
* ``POST /tasks/process`` — called by Cloud Scheduler every minute;
  runs ``Processor.tick()``. Protected by bearer token.
* ``POST /admin/setup`` — (re)sets the webhook for admin + every child
  bot in the registry. One-shot, useful right after deploy. Protected.
* ``GET /health`` — health check with no side effects.
"""

from __future__ import annotations

import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import FastAPI, Header, HTTPException, Request, status
from telegram import Update

from telegram_excerpt.admin import build_admin_application
from telegram_excerpt.config import get_settings
from telegram_excerpt.logging_conf import configure_logging, get_logger
from telegram_excerpt.manager import BotRegistry, PTBApplication
from telegram_excerpt.models import compute_token_hash
from telegram_excerpt.processor import Processor
from telegram_excerpt.storage import FirestoreStorage

log = get_logger(__name__)

_ADMIN_WEBHOOK_SECRET_KEY = "admin_webhook_secret"


# ─── Lifespan ─────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Initialize/shut down storage, registry, admin app."""
    configure_logging(json_output=True)
    settings = get_settings()

    storage = FirestoreStorage(project_id=settings.firestore_project_id)
    registry = BotRegistry(storage)
    await registry.reload()
    await registry.start()

    admin_app = build_admin_application(storage, registry)
    await admin_app.initialize()
    await admin_app.start()

    # Admin bot webhook secret (random per process — regenerated on each
    # deploy, requiring a call to /admin/setup after redeploy).
    from telegram_excerpt.models import generate_webhook_secret

    admin_webhook_secret = generate_webhook_secret()

    processor = Processor(storage=storage, registry=registry)

    app.state.storage = storage
    app.state.registry = registry
    app.state.admin_app = admin_app
    app.state.admin_token_hash = compute_token_hash(
        settings.telegram_admin_bot_token.get_secret_value()
    )
    app.state.admin_webhook_secret = admin_webhook_secret
    app.state.processor = processor
    log.info("web.lifespan.started")

    try:
        yield
    finally:
        await admin_app.stop()
        await admin_app.shutdown()
        await registry.stop()
        await storage.close()
        log.info("web.lifespan.stopped")


# ─── Routes ───────────────────────────────────────────────────────────


def _register_routes(app: FastAPI) -> None:
    @app.get("/health")
    async def health() -> dict[str, Any]:
        result: dict[str, Any] = {"status": "ok"}
        storage: FirestoreStorage | None = getattr(app.state, "storage", None)
        if storage is not None:
            try:
                # Lightweight Firestore ping: list bots collection with limit 1
                async for _ in storage._client.collection("bots").limit(1).stream():
                    pass
                result["firestore"] = "ok"
            except Exception as exc:
                result["firestore"] = f"error: {exc}"
                result["status"] = "degraded"
        return result

    @app.post("/webhook/{token_hash}")
    async def webhook(
        token_hash: str,
        request: Request,
        x_telegram_bot_api_secret_token: Annotated[str | None, Header()] = None,
    ) -> dict[str, str]:
        return await _handle_webhook(
            app=app,
            token_hash=token_hash,
            request=request,
            secret_header=x_telegram_bot_api_secret_token,
        )

    @app.post("/tasks/process")
    async def tasks_process(
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        _require_bearer(authorization)
        processor: Processor = app.state.processor
        result = await processor.tick()
        return result

    @app.post("/admin/setup")
    async def admin_setup(
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        _require_bearer(authorization)
        return await _setup_all_webhooks(app)


# ─── App factory ──────────────────────────────────────────────────────


def create_app() -> FastAPI:
    """FastAPI app factory (useful in tests)."""
    app = FastAPI(
        title="telegram-excerpt",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    _register_routes(app)
    return app


app = create_app()


# ─── Handlers ─────────────────────────────────────────────────────────


async def _handle_webhook(
    *,
    app: FastAPI,
    token_hash: str,
    request: Request,
    secret_header: str | None,
) -> dict[str, str]:
    # Admin?
    if hmac.compare_digest(token_hash, app.state.admin_token_hash):
        if secret_header is None or not hmac.compare_digest(
            secret_header, app.state.admin_webhook_secret
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid webhook secret",
            )
        ptb_app: PTBApplication = app.state.admin_app
    else:
        registry: BotRegistry = app.state.registry
        entry = registry.get_by_hash(token_hash)
        if entry is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown bot")
        cfg, ptb_app = entry
        if secret_header is None or not hmac.compare_digest(secret_header, cfg.webhook_secret):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid webhook secret",
            )

    body = await request.json()
    update = Update.de_json(body, ptb_app.bot)
    if update is None:  # defensive: PTB types it non-optional but body is untrusted
        log.warning("web.webhook.invalid_update")  # type: ignore[unreachable]
        return {"status": "ignored"}
    await ptb_app.process_update(update)
    return {"status": "ok"}


def _require_bearer(header_value: str | None) -> None:
    settings = get_settings()
    assert settings.scheduler_auth_token is not None  # validated in config
    expected = settings.scheduler_auth_token.get_secret_value()
    if not header_value or not header_value.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing bearer")
    provided = header_value.removeprefix("Bearer ").strip()
    if not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid bearer")


async def _setup_all_webhooks(app: FastAPI) -> dict[str, Any]:
    settings = get_settings()
    assert settings.base_url is not None
    base = settings.base_url

    registry: BotRegistry = app.state.registry
    admin_app: PTBApplication = app.state.admin_app
    admin_hash: str = app.state.admin_token_hash
    admin_secret: str = app.state.admin_webhook_secret

    results: dict[str, Any] = {"admin": None, "bots": []}

    # Admin
    try:
        await admin_app.bot.set_webhook(
            url=f"{base}/webhook/{admin_hash}",
            secret_token=admin_secret,
            allowed_updates=Update.ALL_TYPES,
        )
        results["admin"] = "ok"
    except Exception as exc:
        results["admin"] = f"error: {exc}"
        log.error("web.setup.admin.failed", error=str(exc))

    # Child bots
    for cfg in registry.all_configs():
        entry = registry.get(cfg.chat_id)
        if entry is None:
            continue
        _, child_app = entry
        try:
            await child_app.bot.set_webhook(
                url=f"{base}/webhook/{cfg.token_hash}",
                secret_token=cfg.webhook_secret,
                allowed_updates=Update.ALL_TYPES,
            )
            results["bots"].append({"chat_id": cfg.chat_id, "status": "ok"})
        except Exception as exc:
            results["bots"].append({"chat_id": cfg.chat_id, "status": f"error: {exc}"})
            log.error("web.setup.bot.failed", chat_id=cfg.chat_id, error=str(exc))

    return results
