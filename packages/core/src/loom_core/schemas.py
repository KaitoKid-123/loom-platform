"""Model Pydantic dùng chung giữa API, task pod và client sinh tự động."""

from typing import Any

from pydantic import BaseModel, Field


class HealthStatus(BaseModel):
    status: str
    version: str


class ReadyStatus(BaseModel):
    status: str
    checks: dict[str, str] = Field(default_factory=dict)


class CurrentUser(BaseModel):
    subject: str
    email: str
    display_name: str


class ProblemDetail(BaseModel):
    """RFC 9457 Problem Details. `type` để 'about:blank' khi không có trang tài
    liệu riêng cho loại lỗi đó — RFC cho phép, và bịa một URL không tồn tại thì
    tệ hơn."""

    type: str = "about:blank"
    title: str
    status: int
    detail: str | None = None
    instance: str | None = None
    errors: list[dict[str, Any]] | None = None
