# syntax=docker/dockerfile:1

# trixie chứ không bookworm: uv bỏ biến thể bookworm từ 0.11.2, chuyển sang
# Debian 13 (trixie). Hai stage PHẢI cùng một bản Debian — .venv biên dịch ở
# builder được copy sang runtime, lệch bản là lệch ABI glibc.
FROM ghcr.io/astral-sh/uv:0.11.21-python3.12-trixie-slim AS builder

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Dependency layer — only invalidated when pyproject/lock change. loom-api
# now pulls in TWO more workspace packages (vòng đời warehouse: it calls
# `loom_iceberg.warehouse.create_warehouse`/`ensure_bootstrapped` directly —
# see `loom_api.warehouse_provisioning`). loom-iceberg itself depends on
# loom-storage, so both must be present at this layer too, same pattern as
# services/loom-query/Dockerfile.
COPY pyproject.toml uv.lock ./
COPY packages/core/pyproject.toml packages/core/
COPY packages/storagekit/pyproject.toml packages/storagekit/
COPY packages/icebergkit/pyproject.toml packages/icebergkit/
COPY services/api/pyproject.toml services/api/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package loom-api --no-install-project

# Source layer
COPY packages/core packages/core
COPY packages/storagekit packages/storagekit
COPY packages/icebergkit packages/icebergkit
COPY services/api services/api
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package loom-api

FROM python:3.12-slim-trixie AS runtime

RUN useradd --create-home --uid 10001 loom
WORKDIR /app
COPY --from=builder --chown=loom:loom /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER 10001
EXPOSE 8000
# Factory form: the app is only built when the process starts, not at import time
CMD ["uvicorn", "loom_api.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
