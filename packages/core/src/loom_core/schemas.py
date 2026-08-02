"""Model Pydantic dùng chung giữa API, task pod và client sinh tự động."""

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
    """RFC 9457."""

    type: str = "about:blank"
    title: str
    status: int
    detail: str | None = None
