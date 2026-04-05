"""Application entrypoint — dual runtime mode.

``python -m telegram_excerpt``

* ``MODE=webhook`` → starts uvicorn on :data:`web.app` (for Cloud Run).
* ``MODE=polling`` → starts every bot in long-polling + asyncio scheduler
  that invokes ``Processor.tick()`` periodically (for local dev).
"""

from __future__ import annotations

import asyncio
import signal

from telegram_excerpt.config import Mode, get_settings
from telegram_excerpt.logging_conf import configure_logging, get_logger

log = get_logger(__name__)


def main() -> None:
    """console_scripts entry point."""
    settings = get_settings()
    configure_logging(json_output=(settings.mode is Mode.WEBHOOK))
    log.info("main.start", mode=settings.mode.value)

    if settings.mode is Mode.WEBHOOK:
        _run_webhook()
    else:
        asyncio.run(_run_polling())


def _run_webhook() -> None:
    """Start uvicorn pointing at ``telegram_excerpt.web:app``."""
    # Cloud Run injects the PORT env var.
    import os

    import uvicorn

    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(
        "telegram_excerpt.web:app",
        host="0.0.0.0",  # noqa: S104 — Cloud Run requires binding to 0.0.0.0
        port=port,
        log_config=None,  # keep structlog
        access_log=False,
    )


async def _run_polling() -> None:
    """Polling loop for local dev."""
    from telegram_excerpt.admin import build_admin_application
    from telegram_excerpt.manager import BotRegistry
    from telegram_excerpt.processor import Processor
    from telegram_excerpt.storage import FirestoreStorage

    settings = get_settings()
    storage = FirestoreStorage(project_id=settings.firestore_project_id)
    registry = BotRegistry(storage)
    await registry.reload()
    await registry.start()

    admin_app = build_admin_application(storage, registry)
    await admin_app.initialize()
    await admin_app.start()
    assert admin_app.updater is not None
    await admin_app.updater.start_polling()

    processor = Processor(storage=storage, registry=registry)

    stop_event = asyncio.Event()

    def _signal_handler() -> None:
        log.info("main.signal.received")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:  # pragma: no cover
            # Windows: fall back to KeyboardInterrupt
            signal.signal(sig, lambda *_: _signal_handler())

    log.info(
        "main.polling.started",
        scheduler_interval=settings.polling_scheduler_interval_seconds,
    )

    try:
        while not stop_event.is_set():
            try:
                await processor.tick()
            except Exception as exc:
                log.exception("main.tick.failed", error=str(exc))
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=settings.polling_scheduler_interval_seconds,
                )
            except TimeoutError:
                continue
    finally:
        log.info("main.polling.stopping")
        await admin_app.updater.stop()
        await admin_app.stop()
        await admin_app.shutdown()
        await registry.stop()
        await storage.close()
        log.info("main.polling.stopped")


if __name__ == "__main__":
    main()
