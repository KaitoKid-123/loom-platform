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
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/home/loom

# httpfs NẠP SẴN lúc build (Task 13, đường đọc `Files/`) — container chạy
# `readOnlyRootFilesystem: true` (`query-deployment.yaml`), nên `INSTALL
# httpfs` lúc RUNTIME sẽ cố ghi file extension vào một filesystem chỉ đọc, và
# cụm không chắc có egress ra ngoài để tải nó lần đầu. `LOAD` (khác `INSTALL`)
# chỉ ĐỌC file đã có trong `extension_directory` mặc định ($HOME/.duckdb/
# extensions/v<version>/<platform>/) — đã kiểm bằng thực nghiệm: `LOAD httpfs`
# chạy được trên một $HOME chỉ có quyền đọc, không cần `INSTALL` lại trong
# tiến trình runtime, miễn phiên bản DuckDB/nền tảng khớp đúng bản đã `INSTALL`
# ở đây. `runner._install_files_secret` vì vậy CHỈ gọi `LOAD httpfs`.
#
# Chạy SAU khi chuyển `USER loom` (không phải root): `HOME=/home/loom` phải
# TRÙNG với `HOME` mà tiến trình runtime thấy (USER 10001 == loom, cùng UID) —
# khác `HOME` giữa lúc build và lúc chạy thì file đã cài nằm sai chỗ, và
# `LOAD` runtime lại phải tự tải qua mạng như chưa từng cài gì.
USER loom
RUN python -c "import duckdb; duckdb.connect().execute('INSTALL httpfs')"

USER 10001
EXPOSE 8000
# Factory form: app chỉ dựng khi tiến trình thật sự khởi động, không phải lúc import module.
CMD ["uvicorn", "loom_query.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
