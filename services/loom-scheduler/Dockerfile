# syntax=docker/dockerfile:1

# Cùng khuôn services/loom-query/Dockerfile và services/loom-task/Dockerfile —
# xem docstring ở services/api/Dockerfile cho lý do `trixie` ở CẢ HAI stage (uv
# bỏ biến thể bookworm từ 0.11.2; `.venv` biên dịch ở builder được COPY nguyên
# sang runtime, nên hai stage lệch bản Debian là lệch ABI glibc và lỗi chỉ lộ
# ra lúc chạy). Tag uv ở dòng dưới bị `make check-pins` so với UV_VERSION trong
# deploy/versions.env.
FROM ghcr.io/astral-sh/uv:0.11.21-python3.12-trixie-slim AS builder

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Dependency layer — chỉ vô hiệu khi pyproject/lock đổi. loom-scheduler kéo
# theo ĐÚNG MỘT package workspace: `loom-core` (`services/loom-scheduler/
# pyproject.toml`), và bản thân `loom-core` không phụ thuộc member workspace nào
# (`packages/core/pyproject.toml` chỉ khai croniter/pydantic/pydantic-settings).
# Một cây phụ thuộc ngắn hơn hẳn loom-query (bốn) và loom-task (bốn) — và đó là
# HỆ QUẢ TRỰC TIẾP của hai tính chất mà `tests/test_no_db_no_k8s.py` canh: không
# database và không k8s nghĩa là không icebergkit, không storagekit, không
# sqlkit, không client Kubernetes.
#
# Thiếu một pyproject.toml ở layer này thì `uv sync` hỏng NGAY tại đây với
# "workspace member not found", không phải một lỗi import lúc chạy.
COPY pyproject.toml uv.lock ./
COPY packages/core/pyproject.toml packages/core/
COPY services/loom-scheduler/pyproject.toml services/loom-scheduler/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package loom-scheduler --no-install-project

# Source layer
COPY packages/core packages/core
COPY services/loom-scheduler services/loom-scheduler
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package loom-scheduler

FROM python:3.12-slim-trixie AS runtime

RUN useradd --create-home --uid 10001 loom
WORKDIR /app
COPY --from=builder --chown=loom:loom /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER 10001
# KHÔNG `EXPOSE`, KHÔNG server: tiến trình này chỉ GỬI request, không nhận. Hệ
# quả ở chart: `scheduler-deployment.yaml` không có `ports`, không có Service và
# không có probe HTTP nào — xem docstring ở đó.
#
# `python -m` chứ không một console script, cùng lý do đã ghi ở
# `services/loom-task/Dockerfile`: entry point phải được cài đúng vào .venv để
# tồn tại, còn `-m` chỉ cần module có mặt — một lỗi đóng gói lộ ra thành
# "No module named loom_scheduler", câu chỉ thẳng chỗ cần sửa.
CMD ["python", "-m", "loom_scheduler.main"]
