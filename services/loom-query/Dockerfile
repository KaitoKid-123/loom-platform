# syntax=docker/dockerfile:1

# Cùng khuôn với services/api/Dockerfile — xem docstring ở đó cho lý do trixie
# ở cả hai stage (uv bỏ biến thể bookworm từ 0.11.2; .venv biên dịch ở builder
# được copy sang runtime nên hai stage PHẢI cùng một bản Debian, không thì lệch
# ABI glibc).
FROM ghcr.io/astral-sh/uv:0.11.21-python3.12-trixie-slim AS builder

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Dependency layer — chỉ vô hiệu khi pyproject/lock đổi. loom-query kéo theo
# BA package workspace (loom-core trực tiếp; loom-sql và loom-iceberg — bản
# thân loom-iceberg lại kéo loom-storage — nên cả bốn phải có mặt ở COPY layer
# này, không chỉ loom-core).
COPY pyproject.toml uv.lock ./
COPY packages/core/pyproject.toml packages/core/
COPY packages/sqlkit/pyproject.toml packages/sqlkit/
COPY packages/storagekit/pyproject.toml packages/storagekit/
COPY packages/icebergkit/pyproject.toml packages/icebergkit/
COPY services/loom-query/pyproject.toml services/loom-query/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package loom-query --no-install-project

# Source layer
COPY packages/core packages/core
COPY packages/sqlkit packages/sqlkit
COPY packages/storagekit packages/storagekit
COPY packages/icebergkit packages/icebergkit
COPY services/loom-query services/loom-query
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package loom-query

FROM python:3.12-slim-trixie AS runtime

RUN useradd --create-home --uid 10001 loom
WORKDIR /app
COPY --from=builder --chown=loom:loom /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER 10001
EXPOSE 8000
# Factory form: app chỉ dựng khi tiến trình thật sự khởi động, không phải lúc import module.
CMD ["uvicorn", "loom_query.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
