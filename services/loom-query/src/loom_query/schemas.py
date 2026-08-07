"""Model Pydantic ra/vào HTTP của `loom-query`."""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict

from loom_core.schemas import Principal


class QueryCreate(BaseModel):
    """Body của `POST /api/v1/query`."""

    model_config = ConfigDict(extra="forbid")

    lakehouse_id: uuid.UUID
    sql: str

    # TẠM THỜI — xem docstring `main.py`. `loom-api` CHƯA chuyển tiếp request
    # này (đó là việc của task sau); tới lúc đó, principal của người dùng cuối
    # phải tới qua đúng MỘT đường (session cookie mà `loom-api` đã xác thực),
    # còn ở đây nó tới thẳng trong thân request. Khi `loom-api` bắt đầu proxy,
    # trường này PHẢI bị gỡ khỏi bề mặt công khai của `loom-query` — giữ nó lại
    # nghĩa là bất kỳ ai gọi thẳng `loom-query` (bỏ qua `loom-api`) cũng tự
    # xưng được là bất kỳ ai.
    principal: Principal


class QueryCreated(BaseModel):
    query_id: uuid.UUID


class ColumnOut(BaseModel):
    name: str
    type: str


class QueryStatusOut(BaseModel):
    """Đáp ứng của `GET /api/v1/query/{id}`.

    `columns`/`rows`/`error` là `None` chừng nào chưa có gì để nói — route dùng
    `response_model_exclude_none=True` để chúng biến mất khỏi JSON thay vì
    hiện ra là `null`, cùng quy ước với `ProblemDetail` bên `loom-api`.
    """

    status: str
    columns: list[ColumnOut] | None = None
    rows: list[list[Any]] | None = None
    error: str | None = None
