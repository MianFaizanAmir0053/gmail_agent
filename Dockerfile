# syntax=docker/dockerfile:1

FROM python:3.12-slim AS builder

# TODO: pin to a specific uv release once the toolchain settles.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Dependency layer first so source edits do not bust the cache.
COPY pyproject.toml README.md ./
COPY app ./app
RUN uv sync --no-dev


FROM python:3.12-slim AS runtime

RUN useradd --create-home --uid 10001 appuser
WORKDIR /app

COPY --from=builder --chown=appuser:appuser /app /app
# Migrations ship with the image: the app applies them at boot when
# MIGRATE_ON_BOOT is set, and they must exist inside the container to do that.
COPY --chown=appuser:appuser migrations ./migrations

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000

USER appuser
EXPOSE 8000

# Shell form so $PORT expands -- most platforms assign the port at runtime and
# a hardcoded one silently fails their health checks.
CMD uvicorn app.api:app --host 0.0.0.0 --port ${PORT}
