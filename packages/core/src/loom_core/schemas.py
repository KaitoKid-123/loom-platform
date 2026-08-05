"""Model Pydantic dùng chung giữa API, task pod và client sinh tự động."""

import uuid
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class HealthStatus(BaseModel):
    status: str
    version: str


class ReadyStatus(BaseModel):
    status: str
    checks: dict[str, str] = Field(default_factory=dict)


class Principal(BaseModel):
    """Danh tính đã xác thực, kèm mọi thứ RBAC cần. `groups` là tuple đã chuẩn
    hoá để dùng làm phần của cache key trong phạm vi request."""

    model_config = ConfigDict(frozen=True)

    user_id: uuid.UUID
    subject: str
    email: str
    display_name: str
    groups: tuple[str, ...] = ()

    @field_validator("subject")
    @classmethod
    def _subject_not_blank(cls, value: str) -> str:
        # Bất biến nằm trên chính kiểu dữ liệu, không nằm ở verify(): load_session()
        # dựng Principal THẲNG từ hàng trong DB, tức là đi vòng qua verify() hoàn
        # toàn. IdTokenClaims.__post_init__ giữ đúng bất biến này cho đường token;
        # đây là bản sao của nó cho đường database.
        if not value.strip():
            raise ValueError("subject không được rỗng")
        return value

    @field_validator("groups", mode="before")
    @classmethod
    def _normalise(cls, value: object) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, str):
            # Một chuỗi cũng iterate được: "admins" sẽ lặng lẽ thành sáu nhóm
            # một-ký-tự thay vì một nhóm.
            raise ValueError("groups phải là danh sách, không phải chuỗi")
        if not isinstance(value, Iterable):
            raise ValueError("groups phải là danh sách")
        names = [str(v).strip() for v in value]
        if any(not n for n in names):
            raise ValueError("tên nhóm không được rỗng")
        # sorted() làm thứ tự KHÔNG phụ thuộc IdP và không phụ thuộc thứ tự băm
        # của set — cả hai đều đủ để làm cache key trong phạm vi request lệch.
        return tuple(sorted(set(names)))


class CurrentUser(BaseModel):
    subject: str
    email: str
    display_name: str
    groups: tuple[str, ...] = ()


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


class WorkspaceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # `name` là định danh KỸ THUẬT và nó đi vào `storage_prefix` ở Giai đoạn 2.
    # Chặn khoảng trắng và chữ hoa ngay tại biên rẻ hơn nhiều so với migrate
    # prefix sau này.
    name: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9-]*$")
    display_name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    domain_id: uuid.UUID | None = None


class WorkspaceOut(BaseModel):
    id: uuid.UUID
    name: str
    display_name: str
    description: str | None
    domain_id: uuid.UUID | None
    # Vai trò hiệu lực của CHÍNH người gọi, không phải vai trò cao nhất có trong
    # workspace. Frontend dùng nó để ẩn nút mà server sẽ từ chối — thiếu nó thì
    # người dùng bấm rồi ăn 403 mà không hiểu vì sao.
    my_role: str


class ItemCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str
    name: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9-]*$")
    display_name: str = Field(min_length=1, max_length=255)
    # Phải bắt đầu VÀ kết thúc bằng `/`. Nhận cả `/a/b` lẫn `/a/b/` thì cây trên
    # UI hiện hai nhánh cho cùng một folder và không ai hiểu vì sao.
    folder_path: str = Field(default="/", max_length=1024, pattern=r"^/([^/]+/)*$")
    description: str | None = Field(default=None, max_length=2000)
    definition: dict[str, Any]


class ItemPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, min_length=1, max_length=255)
    folder_path: str | None = Field(default=None, max_length=1024, pattern=r"^/([^/]+/)*$")
    description: str | None = Field(default=None, max_length=2000)
    definition: dict[str, Any] | None = None
    change_note: str | None = Field(default=None, max_length=500)


class ItemOut(BaseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    type: str
    name: str
    display_name: str
    folder_path: str
    description: str | None
    definition: dict[str, Any]
    version: int
    updated_at: datetime


class PageOut(BaseModel):
    items: list[Any]
    next_cursor: str | None = None
