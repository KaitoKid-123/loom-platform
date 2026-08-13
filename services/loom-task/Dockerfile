# syntax=docker/dockerfile:1

# Cùng khuôn services/api/Dockerfile và services/loom-query/Dockerfile — xem
# docstring ở đó cho lý do trixie ở CẢ HAI stage (uv bỏ biến thể bookworm từ
# 0.11.2; .venv biên dịch ở builder được copy sang runtime nên lệch bản Debian là
# lệch ABI glibc). Tag uv ở dòng dưới bị `make check-pins` so với UV_VERSION
# trong deploy/versions.env.
FROM ghcr.io/astral-sh/uv:0.11.21-python3.12-trixie-slim AS builder

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Dependency layer — chỉ vô hiệu khi pyproject/lock đổi. loom-task kéo theo BỐN
# package workspace: loom-core, loom-connector và loom-iceberg TRỰC TIẾP (xem
# `services/loom-task/pyproject.toml`), cộng loom-storage BẮC CẦU qua
# loom-iceberg (xem `packages/icebergkit/pyproject.toml`). Cả bốn phải có mặt ở
# layer này. Thiếu một cái — kể cả cái bắc cầu mà service này không hề import —
# thì `uv sync` hỏng NGAY tại đây với "workspace member not found", không phải
# một lỗi import lúc chạy. Cùng cạm bẫy mà `services/loom-query/Dockerfile` ghi
# lại cho đúng cặp loom-iceberg/loom-storage này.
COPY pyproject.toml uv.lock ./
COPY packages/core/pyproject.toml packages/core/
COPY packages/connectorkit/pyproject.toml packages/connectorkit/
COPY packages/storagekit/pyproject.toml packages/storagekit/
COPY packages/icebergkit/pyproject.toml packages/icebergkit/
COPY services/loom-task/pyproject.toml services/loom-task/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package loom-task --no-install-project

# Source layer
COPY packages/core packages/core
COPY packages/connectorkit packages/connectorkit
COPY packages/storagekit packages/storagekit
COPY packages/icebergkit packages/icebergkit
COPY services/loom-task services/loom-task
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package loom-task

FROM python:3.12-slim-trixie AS runtime

RUN useradd --create-home --uid 10001 loom
WORKDIR /app
COPY --from=builder --chown=loom:loom /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER 10001
# KHÔNG `EXPOSE`, KHÔNG server: tiến trình này chạy MỘT lần cho MỘT `ingest_run`
# rồi thoát (xem `loom_task.main`). Kubernetes phóng nó bằng `Job`, không bằng
# `Deployment` — `loom_api.jobs.JobLauncher` là bên dựng, kèm
# `restart_policy="Never"` và `backoff_limit=0`.
#
# `python -m` chứ không một console script: entry point phải được cài đặt đúng
# vào .venv để tồn tại, còn `-m` chỉ cần module có mặt — một lỗi đóng gói lộ ra
# thành "No module named loom_task", câu chỉ đúng chỗ cần sửa.
CMD ["python", "-m", "loom_task.main"]
