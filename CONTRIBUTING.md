# Contributing

Thanks for your interest in contributing to **telegram-excerpt**!

## Getting started

```bash
git clone https://github.com/oreste/telegram-excerpt.git
cd telegram-excerpt
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pre-commit install
```

## Development workflow

1. Create a branch from `main`.
2. Make your changes.
3. Run the checks locally:

```bash
ruff check src tests
ruff format src tests
mypy src
pytest
```

4. Open a pull request against `main`.

CI runs the same checks plus `pip-audit` (dependency security) and a
Docker build. All checks must pass before merge.

## Code style

- **Ruff** handles linting and formatting (config in `pyproject.toml`).
- **Mypy strict** — all code must pass `mypy --strict`.
- **Google-style docstrings** for public functions.
- **Structured logging** via `structlog` — never `print()`.
- User-facing text (help strings, bot replies) is in **Italian** by
  project convention.

## Type annotations

- Use `# type: ignore[error-code]` with a specific code, never bare
  `# type: ignore`.
- For PTB's `Application` type, import the `PTBApplication` alias from
  `manager.py` instead of using the raw 6-parameter generic.

## Tests

- Tests live in `tests/` and use `pytest` + `pytest-asyncio`.
- Mock external services (Telegram API, Firestore, OpenRouter) — tests
  must run offline.
- Call `get_settings.cache_clear()` after modifying env vars in tests.

## Commits

- Use [Conventional Commits](https://www.conventionalcommits.org/)
  (`feat:`, `fix:`, `docs:`, `ci:`, `test:`, `refactor:`, `style:`,
  `chore:`).
- Keep commits focused — one logical change per commit.

## Reporting issues

Use [GitHub Issues](https://github.com/oreste/telegram-excerpt/issues).
For security vulnerabilities, see [SECURITY.md](SECURITY.md).
