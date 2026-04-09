# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Multi-bot Telegram monitor that buffers group messages in Firestore and, after
`BATCH_SILENCE_SECONDS` of silence, sends the batch to an LLM (OpenRouter) to
decide whether to generate PRDs. PRDs are delivered to the admin as `.md` files.
Supports **webhook** mode (Cloud Run) and **polling** mode (docker-compose dev).

## Tech Stack

- **Language:** Python 3.12+
- **Telegram:** python-telegram-bot v21 (async)
- **Web:** FastAPI + uvicorn (MODE=webhook only)
- **LLM:** OpenRouter via `openai` SDK (base_url override) — do NOT use the Anthropic SDK.
- **Storage:** Google Cloud Firestore (`google-cloud-firestore` async client)
- **Config:** pydantic-settings (validation at-startup)
- **Logging:** structlog (JSON in prod, colored console in dev)
- **Deployment:** Docker / Cloud Run

## Architecture

Module `src/telegram_excerpt/`:

- `__main__.py` — entrypoint; branches on `MODE` env var.
- `config.py` — pydantic `Settings` with validation.
- `logging_conf.py` — structlog setup.
- `models.py` — `BotConfig`, `BufferedMessage`, `ClassifyResult`, `PRDDoc`.
- `exceptions.py` — custom hierarchy.
- `storage.py` — async Firestore: bot registry + message buffer.
- `manager.py` — `BotRegistry`: cache of PTB Applications + lifecycle.
- `admin.py` — admin bot handlers (`/add_bot`, `/remove_bot`, `/list_bots`, `/set_n`).
- `processor.py` — `flush_if_silent`: fetch → classify → generate → send → cleanup.
- `llm.py` — `classify_batch()` + `generate_prds()` via OpenRouter JSON mode.
- `web.py` — FastAPI app (MODE=webhook): `/webhook/{hash}`, `/tasks/process`, `/admin/setup`, `/health`.

## Firestore schema

- `bots/{chat_id}` — `BotConfig` (token, chat_id, n, last_read_message_id, has_pending, webhook_secret, enabled).
- `bots/{chat_id}/buffer/{message_id}` — `BufferedMessage`.

## Commands

```bash
# Run locally (polling mode)
docker-compose up --build

# Run entrypoint directly (requires .env loaded + secrets/firebase.json)
pip install -e .[dev]
python -m telegram_excerpt

# Lint + type check
ruff check src tests
ruff format --check src tests
mypy src

# Tests
pytest

# Rebuild after dependency changes
docker-compose build --no-cache
```

## Key Conventions

- **Async everywhere**: PTB handlers, Firestore client, OpenRouter.
- **Type hints mandatory** (`mypy --strict` in CI).
- **Google-style docstrings**; ruff applies pydocstyle.
- **Structured logging** (never `print()`).
- **Prompt templates** are module-level constants in `llm.py`.
- **Italian prompts / user-facing text** are intentional — fork and
  localize when reusing the repo.
- **Secrets**: never commit `.env` or the service-account JSON. Use `.env.example`.
- **Admin auth**: only `FORWARD_CHAT_ID` can use the commands; other users
  are silently ignored.
- **Webhook security**: always validate `X-Telegram-Bot-Api-Secret-Token` +
  bearer on `/tasks/*` and `/admin/*` endpoints.
- **Flush idempotency**: `set_last_read` **after** PRD send, **before**
  `clear_buffer_up_to`.

## Gotchas

- `python3 -m py_compile src/telegram_excerpt/*.py tests/*.py` — quick syntax check without installing deps.
- Avoid Telegram MarkdownV2 parse_mode in admin replies: too many chars need escaping (`_ * [ ] ( ) . ! -` …) → malformed text causes silent `BadRequest`. Use plain text.
- PTB multi-handler: buffer handler in `group=0`, optional responder in `group=1`. Handlers in different groups all run; within one group only the first match runs.
- Child bots return `403 Forbidden` when sending to a user who hasn't clicked /start on them. Admin must /start each child bot once; `/add_bot` reply includes the reminder.
- Firestore requires a composite index on `bots` for (enabled ASC, has_pending ASC, last_message_ts ASC) — needed by `storage.list_silent_bots`. First-run error log contains a link; if it doesn't auto-fill, create manually from the Firestore console.
- `.dockerignore` must keep `!README.md` exception — `pyproject.toml` declares `readme = "README.md"` and `pip install .` fails without it.
- In tests, call `get_settings.cache_clear()` after `monkeypatch.setenv(...)` — `get_settings` is `lru_cache`d.
- Telegram Bot API has no "fetch history": bots only see messages sent *after* joining. The buffer in Firestore is the source of truth for processing.
