# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] — 2026-04-05

### Added
- Multi-bot registry persisted on Firestore.
- Admin commands `/add_bot`, `/remove_bot`, `/list_bots`, `/set_n`, `/help`.
- Per-bot message buffer with flush after N minutes of silence
  (configurable, default 180s).
- Two-step LLM pipeline: `classify_batch` + `generate_prds` via OpenRouter.
- PRDs delivered as `.md` files to `FORWARD_CHAT_ID` with caption
  containing author, timestamp and group.
- Dual-mode runtime: `polling` (local dev) and `webhook` (Cloud Run).
- FastAPI web layer with endpoints `/webhook/{hash}`, `/tasks/process`,
  `/admin/setup`, `/health`.
- Structured logging (structlog), strict type hints (mypy), ruff linting.
- GitHub Actions CI: lint, type check, test, security audit, docker build.
- Full documentation: README, ARCHITECTURE, LOCAL_DEV, DEPLOY_CLOUD_RUN.

[Unreleased]: https://github.com/oreste/telegram-excerpt/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/oreste/telegram-excerpt/releases/tag/v0.1.0
