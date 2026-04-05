FROM python:3.14-slim AS base

# Prevent Python from writing .pyc files + enable unbuffered stdout
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Runtime user (non-root)
ARG APP_UID=10001
RUN groupadd --system --gid ${APP_UID} app \
 && useradd --system --uid ${APP_UID} --gid app --create-home app

WORKDIR /app

# ─── Dependencies ─────────────────────────────────────────────────────
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir .

# ─── Runtime ──────────────────────────────────────────────────────────
USER app

# Cloud Run inietta PORT=8080. Per polling mode la porta è ignorata.
EXPOSE 8080
ENV PORT=8080

# Entrypoint unico che discrimina su MODE
CMD ["python", "-m", "telegram_excerpt"]
