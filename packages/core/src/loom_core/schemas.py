"""Model Pydantic dùng chung giữa API, task pod và client sinh tự động."""

import uuid
from collections.abc import Iterable
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from loom_core.item_definitions import ItemType


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
            raise ValueError("subject must not be blank")
        return value

    @field_validator("groups", mode="before")
    @classmethod
    def _normalise(cls, value: object) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, str):
            # Một chuỗi cũng iterate được: "admins" sẽ lặng lẽ thành sáu nhóm
            # một-ký-tự thay vì một nhóm.
            raise ValueError("groups must be a list, not a string")
        if not isinstance(value, Iterable):
            raise ValueError("groups must be a list")
        names = [str(v).strip() for v in value]
        if any(not n for n in names):
            raise ValueError("a group name must not be blank")
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


class DomainCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9-]*$")
    display_name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)


class DomainPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)


class DomainOut(BaseModel):
    id: uuid.UUID
    name: str
    display_name: str
    description: str | None
    # Số workspace đang thuộc domain. Một domain rỗng trông giống một domain mới tạo và
    # một domain vừa bị dọn sạch; con số này phân biệt hai thứ đó.
    workspace_count: int
    # Vai trò hiệu lực của NGƯỜI GỌI ở cấp domain — `None` khi họ không có vai trò nào.
    # Khác `WorkspaceOut.my_role` (chuỗi rỗng) vì ở đây "không có vai trò" là trạng thái
    # bình thường: ai cũng thấy được danh sách domain.
    my_role: str | None


class WorkspaceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # `name` là định danh KỸ THUẬT và nó đi vào `storage_prefix` ở Giai đoạn 2.
    # Chặn khoảng trắng và chữ hoa ngay tại biên rẻ hơn nhiều so với migrate
    # prefix sau này.
    name: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9-]*$")
    display_name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    domain_id: uuid.UUID | None = None


class WorkspacePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    domain_id: uuid.UUID | None = None
    # Tách khỏi `domain_id=None`: `None` nghĩa là "không đổi", còn cờ này nghĩa là "gỡ
    # khỏi domain". Gộp hai thứ thì không có cách nào gỡ workspace ra khỏi domain.
    clear_domain: bool = False


class WorkspaceOut(BaseModel):
    id: uuid.UUID
    name: str
    display_name: str
    description: str | None
    domain_id: uuid.UUID | None
    # ETag của workspace. Client cần nó để gửi `If-Match` khi sửa.
    version: int = 1
    # Vai trò hiệu lực của CHÍNH người gọi, không phải vai trò cao nhất có trong
    # workspace. Frontend dùng nó để ẩn nút mà server sẽ từ chối — thiếu nó thì
    # người dùng bấm rồi ăn 403 mà không hiểu vì sao.
    my_role: str


class WorkspaceListOut(BaseModel):
    items: list[WorkspaceOut]
    next_cursor: str | None = None
    # Vai trò của người gọi ở cấp TENANT, hoặc None. `WorkspaceOut.my_role` chỉ nói vai
    # trò trong MỘT workspace, nên không có trường này thì giao diện không biết người
    # dùng có tạo được workspace mới hay không — và đó đúng là lý do trang danh sách
    # workspace từng không có nút tạo nào, dù `POST /workspaces` đã có từ Task 21.
    #
    # Ở ĐÂY chứ không ở `/me`: `/me` cố ý không chạm database và được gọi mỗi lần tải
    # trang, còn endpoint này đã truy vấn sẵn và giao diện đã gọi nó ở đúng trang cần biết.
    tenant_role: str | None = None


class ItemCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # `ItemType` chứ không `str`. Router phải gọi `ItemType(body.type)` để dùng được
    # giá trị này, và với `str` thì một loại không tồn tại đi thẳng tới constructor
    # đó, `ValueError` không ai bắt, và client nhận 500 với một thân phản hồi cố ý
    # không nói gì. Khai đúng kiểu ở biên thì Pydantic trả 422 kèm danh sách loại
    # hợp lệ, tức người gọi biết phải sửa gì.
    type: ItemType
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


class PrincipalRef(BaseModel):
    """Trỏ tới ĐÚNG MỘT principal: một người, hoặc một nhóm.

    Quy tắc "đúng một" nằm ở đây chứ không ở router, để câu trả lời 422 mang theo
    `errors[]` như mọi lỗi validate khác — frontend gắn được lỗi vào đúng ô input.
    Một `HTTPException(422)` ở router chỉ cho một dòng `detail` và ô nào sai thì
    người dùng phải tự đoán. `RoleStore` vẫn giữ phép kiểm của nó: nó gọi được từ
    chỗ khác ngoài HTTP.
    """

    model_config = ConfigDict(extra="forbid")

    user_id: uuid.UUID | None = None
    group: str | None = Field(default=None, min_length=1, max_length=255)

    @model_validator(mode="after")
    def _exactly_one_principal(self) -> "PrincipalRef":
        if (self.user_id is None) == (self.group is None):
            raise ValueError("give exactly one of user_id or group")
        return self


class RoleGrant(PrincipalRef):
    role: Literal["viewer", "contributor", "member", "admin"]


class RoleAssignmentOut(BaseModel):
    principal_type: str
    user_id: uuid.UUID | None
    group: str | None
    role: str


class RoleListOut(BaseModel):
    items: list[RoleAssignmentOut]
    # Vai trò mà NGƯỜI GỌI được phép gán — spec mục 7.3. Thuộc tính của người gọi
    # nên nó ở cấp phản hồi, không lặp trên từng hàng.
    #
    # Đây là lớp phòng vệ THỨ HAI và nó không phải chỗ chặn: `RoleStore.grant` là
    # thứ thật sự chặn leo thang. Gỡ trường này thì UI hiện tuỳ chọn server sẽ từ
    # chối; gỡ phép kiểm ở store thì bất kỳ ai gọi API trực tiếp đều leo thang được.
    grantable_roles: list[str]
