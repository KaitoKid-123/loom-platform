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

    # TẠM THỜI, cùng lý do và cùng số phận với `principal` bên dưới: `loom-api`
    # là nguồn thật của "lakehouse này thuộc workspace nào" (nó có database,
    # `loom-query` thì không — xem docstring `main.py`), nhưng CHƯA proxy
    # request này, nên workspace phải tới qua thân request thay vì `loom-query`
    # tự tra cứu. `run_gate` cần nó để phân giải tên bảng ba phần đúng phạm vi
    # workspace (xem `authz._resolve_item_ids`) — tên lakehouse chỉ duy nhất
    # TRONG một workspace, không phải toàn hệ thống. Khi `loom-api` bắt đầu
    # proxy, trường này đổi từ "người gọi tự khai" thành "loom-api tự điền sau
    # khi đã xác minh `lakehouse_id` thuộc workspace nào", cùng lúc với
    # `principal` đổi từ tự khai sang xác thực qua cookie phiên.
    workspace_id: uuid.UUID

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

    `truncated`/`row_count` (Task 8, giới hạn 3): chỉ có giá trị khi
    `status == "succeeded"`. `truncated=True` nghĩa là `rows` KHÔNG phải toàn
    bộ kết quả — chỉ 10.000 (mặc định, cấu hình được) dòng đầu; `row_count` là
    tổng số dòng THẬT trước khi cắt. Thiếu cờ này thì 10.000 dòng đầu trông y
    hệt toàn bộ kết quả, và một báo cáo dựa trên nó sẽ sai mà không ai biết.
    """

    status: str
    columns: list[ColumnOut] | None = None
    rows: list[list[Any]] | None = None
    error: str | None = None
    truncated: bool | None = None
    row_count: int | None = None
