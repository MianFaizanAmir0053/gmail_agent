# syntax=docker/dockerfile:1

FROM python:3.12-slim AS builder

# Pinned to the release uv.lock was produced with. `latest` let a uv release
# change resolution between two builds of the same commit.
COPY --from=ghcr.io/astral-sh/uv:0.12.19 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# `--locked` installs exactly what uv.lock records, and fails the build if the
# lock no longer matches pyproject.toml. Without the lockfile every build
# re-resolved, so a redeploy could change the checkpoint format underneath
# approvals parked in Postgres.
COPY pyproject.toml uv.lock README.md ./
COPY app ./app
RUN uv sync --locked --no-dev


FROM python:3.12-slim AS runtime

RUN useradd --create-home --uid 10001 appuser
WORKDIR /app

COPY --from=builder --chown=appuser:appuser /app /app
# Migrations ship with the image: the app applies them at boot when
# MIGRATE_ON_BOOT is set, and they must exist inside the container to do that.
COPY --chown=appuser:appuser migrations ./migrations

# Boot writes the OAuth client secret and token here from base64 environment
# variables (app/bootstrap.py). WORKDIR created /app as root, so the directory
# is made and handed to appuser explicitly; otherwise that first write fails
# with a PermissionError and the app never starts.
RUN mkdir -p /app/secrets && chown appuser:appuser /app/secrets

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000

USER appuser
EXPOSE 8000

# Shell form so $PORT expands -- most platforms assign the port at runtime and
# a hardcoded one silently fails their health checks.
CMD uvicorn app.api:app --host 0.0.0.0 --port ${PORT}
