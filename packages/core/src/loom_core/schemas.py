"""Model Pydantic dùng chung giữa API, task pod và client sinh tự động."""

import uuid
from collections.abc import Iterable
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from loom_core.cursor import parse_cursor_value
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


class AuthzItemsRequest(BaseModel):
    """Body của `POST /internal/authz/items`.

    `principal` do `loom-query` CHUYỂN TIẾP nguyên trạng, không tự dựng: nó
    không xác thực người dùng cuối, `loom-api` đã làm việc đó khi phát phiên.
    Endpoint này vì vậy không có `PrincipalDep` — xem `routers/internal.py`.
    """

    model_config = ConfigDict(extra="forbid")

    principal: Principal
    item_ids: list[uuid.UUID]


class AuthzItemsResponse(BaseModel):
    """`roles[str(item_id)]` là `None` cho CẢ item không tồn tại LẪN item người
    gọi không có quyền đọc — hai trường hợp đó KHÔNG được phân biệt được từ phía
    gọi (xem docstring `routers/internal.py`). Phân biệt chúng là rò rỉ sự tồn
    tại, đúng loại lỗ mà quy tắc 404-trước-403 của Giai đoạn 1 sinh ra để chặn.

    Khoá kiểu `str`, không `uuid.UUID`: JSON không có khoá UUID, và khai tường
    minh hình dạng thật sự đi qua dây thì đáng tin hơn là dựa vào việc pydantic
    tự đổi khoá hộ lúc serialize.
    """

    roles: dict[str, str | None]


class LakehouseResolveRequest(BaseModel):
    """Body của `POST /internal/lakehouses/resolve` — xem docstring endpoint
    (`routers/internal.py`) cho lý do endpoint này tồn tại tách riêng khỏi
    `/internal/authz/items` và vì sao nó KHÔNG kiểm quyền.

    `names` là cả DANH SÁCH cho một request, không phải một tên một request:
    một câu `JOIN` ba phần chạm N tên lakehouse khác nhau phải đi ĐÚNG MỘT
    round trip, không phải N.
    """

    model_config = ConfigDict(extra="forbid")

    workspace_id: uuid.UUID
    names: list[str]


class QuerySubmitRequest(BaseModel):
    """Body của `POST /api/v1/query` — cổng `loom-api` PHÍA TRÌNH DUYỆT
    (Task 10/11, xem `loom_api.routers.query`).

    Khác `loom-query`'s bản thân của nó (một schema RIÊNG, nội bộ, không phải
    kiểu này — xem docstring `loom_query.schemas.QueryCreate`): request NÀY
    không có `principal`. `loom-api` tự đọc principal từ cookie phiên
    (`PrincipalDep`) — trình duyệt không tự khai được danh tính của chính nó
    ở tầng này, đúng lời hứa của Giai đoạn 1 (một mặt xác thực DUY NHẤT).

    `workspace_id` CÓ MẶT nhưng LUÔN bị bỏ qua: `loom-api` tự tra
    `lakehouse_id` (một `Item` loại `lakehouse`) thuộc workspace nào rồi điền
    giá trị THẬT khi chuyển tiếp sang `loom-query` — không bao giờ dùng giá
    trị người gọi tự khai ở đây. Trường vẫn được khai (không bị `extra=
    "forbid"` chặn) để một client gửi kèm nó không ăn 422 vô nghĩa; giữ nó lại
    dưới dạng "được chấp nhận nhưng bị phớt lờ" là cố ý, KHÔNG phải sót — xem
    docstring `routers/query.py` cho lý do tin giá trị này là một lỗ hổng
    (client gửi `workspace_id` của workspace KHÁC thì phân giải tên bảng ba
    phần sẽ chạy sai phạm vi).
    """

    model_config = ConfigDict(extra="forbid")

    lakehouse_id: uuid.UUID
    sql: str
    workspace_id: uuid.UUID | None = None


class IngestStartRequest(BaseModel):
    """Body của `POST /api/v1/lakehouses/{lakehouse_id}/ingest` (Giai đoạn 3a).

    `lakehouse_id` CỐ Ý không có mặt ở đây: nó nằm trên đường dẫn, và cổng
    quyền (`item.update`) hỏi đúng giá trị đó. Nhận thêm một bản sao trong thân
    request là mở đường cho hai giá trị lệch nhau — và khi chúng lệch, một bên
    đã đi qua cổng quyền còn bên kia thì chưa. Cùng lớp lỗi mà
    `QuerySubmitRequest.workspace_id` mô tả ở trên, chỉ khác là ở đây tránh
    được hẳn bằng cách không khai trường nào.

    `mode` là `Literal` chứ không `str`: một mode lạ phải là 422 ở BIÊN, trước
    khi có hàng `ingest_run` nào được tạo và trước khi có Job nào được phóng.
    Một `if mode not in (...)` trong handler cho cùng kết quả hôm nay và lặng
    lẽ thôi bảo vệ vào ngày ai đó thêm mode thứ ba ở một chỗ mà quên chỗ kia.

    `stream` giới hạn 255 ký tự để khớp cột `ingest_run.stream` — dài hơn thì
    hỏng ở Postgres dưới dạng một 500, còn ở đây nó là một 422 chỉ đúng ô sai.
    """

    model_config = ConfigDict(extra="forbid")

    connection_id: uuid.UUID
    stream: str = Field(min_length=1, max_length=255)
    mode: Literal["full", "incremental"]


class IngestRunAccepted(BaseModel):
    """202 của `POST .../ingest` — chỉ `run_id`, và đó là đủ.

    202 chứ không 201: hàng `ingest_run` đã tồn tại thật, nhưng việc nó mô tả
    thì chưa xong — Job vừa mới được yêu cầu. Mọi thứ khác (trạng thái, số
    dòng, lỗi) đọc qua `GET /ingest/{run_id}` ở Task 13; trả một bản chụp
    trạng thái ngay tại đây chỉ là trả lại chuỗi `"pending"` mà người gọi đã
    biết trước.
    """

    run_id: uuid.UUID


class IngestSourceSpec(BaseModel):
    """Cách TỚI nguồn, và KHÔNG BAO GIỜ cách MỞ nó.

    Đây là bản chiếu có chủ đích của `ConnectionDefinition` với đúng một trường
    bị bỏ đi: `secret_ref`. Pod nạp đã nhận credential nguồn qua `envFrom` từ
    k8s Secret (xem `JobLauncher.launch`), nên nó KHÔNG cần gì thêm ở đây — và
    lời hứa "control plane không đọc credential nguồn" (spec mục 5.2) sụp đổ
    ÂM THẦM nếu spec bắt đầu mang theo dù chỉ là con trỏ tới nó: mọi thứ vẫn
    chạy y nguyên, chỉ là bí mật đã đi qua một đường nó không cần đi qua.
    `test_the_spec_never_mentions_a_password_or_a_secret` canh đúng điều đó.

    KHÔNG dùng thẳng `ConnectionDefinition` rồi `exclude={"secret_ref"}` lúc
    serialize: `exclude` là một tham số ở CHỖ GỌI, nên nó vắng mặt ở lần gọi
    thứ hai mà không ai thấy. Một kiểu riêng không có ô đó thì không có gì để
    quên loại trừ.
    """

    kind: str
    host: str
    port: int
    database: str | None = None


class IngestSpec(BaseModel):
    """`GET /internal/ingest/{run_id}/spec` — mọi thứ pod nạp cần, không hơn.

    `cursor_*` là watermark ĐANG lưu cho stream này, hoặc cả ba đều `None` khi
    chưa có lần nạp nào (hoặc khi `mode="full"`). Ba trường đi CÙNG NHAU và
    luôn cùng nhau: một `cursor_value` không kèm `cursor_type` là một chuỗi
    không so sánh được (xem `loom_core.cursor`), còn không kèm `cursor_column`
    thì không biết áp vào cột nào.

    `workspace_id` có mặt để pod ghi vào đúng prefix storage; nó lấy TỪ hàng
    `ingest_run` (được điền từ lakehouse lúc tạo run), không phải từ pod.

    `connection_id` có mặt vì cột bronze `_source` LÀ nó (spec 3a mục 5.5: "biết
    dòng tới từ nguồn nào"), và pod không có đường nào khác để biết: nó chỉ được
    cấp `run_id`, và bảng `ingest_run` thì nó không đọc được (không có credential
    Postgres control plane — xem `routers/internal_ingest.py`). Một id, không
    phải một bí mật: `test_the_spec_never_mentions_a_password_or_a_secret` quét
    toàn bộ thân phản hồi và trường này nằm trong phạm vi quét đó.
    """

    run_id: uuid.UUID
    lakehouse_id: uuid.UUID
    workspace_id: uuid.UUID
    connection_id: uuid.UUID
    stream: str
    mode: Literal["full", "incremental"]
    source: IngestSourceSpec
    cursor_column: str | None = None
    cursor_type: str | None = None
    cursor_value: str | None = None


class IngestProgressReport(BaseModel):
    """`POST /internal/ingest/{run_id}/progress` — một lô đã hạ cánh.

    **`cursor_type` đi CÙNG `cursor_value`, và đó là điều kiện để luật "watermark
    chỉ tiến" có nghĩa.** `stream_state.cursor_value` là `Text`, nên không có
    `cursor_type` thì so sánh duy nhất làm được là so CHUỖI — và so chuỗi trên
    một cursor `bigint` làm watermark kẹt vĩnh viễn ở lần đầu vượt mốc đổi số
    chữ số (`"1000" > "400"` là `False`). Xem docstring `loom_core.cursor`.

    Cả ba phép kiểm dưới đây nằm Ở BIÊN, không trong handler: một `cursor_type`
    lạ hay một `cursor_value` không đọc được phải là 422 kèm `errors[]` chỉ
    đúng ô sai, TRƯỚC khi chạm database — cùng lý do `IngestStartRequest.mode`
    là `Literal` chứ không một `if` trong handler.

    `rows` là số dòng của LÔ NÀY, không phải tổng tích luỹ: pod không đọc lại
    `ingest_run.rows_written` nên nó không biết tổng, và bắt nó tự cộng dồn
    biến một lần gửi lại thành một lần đếm đè lên nhau.
    """

    model_config = ConfigDict(extra="forbid")

    rows: int = Field(ge=0)
    cursor_column: str | None = Field(default=None, max_length=255)
    cursor_type: str | None = None
    cursor_value: str | None = None

    @model_validator(mode="after")
    def _cursor_fields_travel_together(self) -> "IngestProgressReport":
        present = [
            name
            for name, value in (
                ("cursor_column", self.cursor_column),
                ("cursor_type", self.cursor_type),
                ("cursor_value", self.cursor_value),
            )
            if value is not None
        ]
        if present and len(present) != 3:
            raise ValueError(
                "cursor_column, cursor_type and cursor_value must be sent together "
                f"or not at all; got only {sorted(present)}"
            )
        return self

    @model_validator(mode="after")
    def _cursor_value_matches_its_type(self) -> "IngestProgressReport":
        if self.cursor_type is None or self.cursor_value is None:
            return self
        # Cả hai ngoại lệ đều là lớp con của `ValueError`, nên pydantic gom
        # chúng vào `errors[]` như mọi lỗi validate khác — thông báo của chúng
        # (kiểu nào hợp lệ / vì sao giá trị không đọc được) đi thẳng tới người
        # gọi thay vì bị thay bằng một câu chung chung.
        parse_cursor_value(self.cursor_type, self.cursor_value)
        return self


class IngestCompletionReport(BaseModel):
    """`POST /internal/ingest/{run_id}/complete` — run đã kết thúc, theo hướng nào.

    `error` BẮT BUỘC khi `status="failed"`. Một run `failed` không kèm lý do
    không dẫn người vận hành đi đâu cả: pod đã bị dọn (`ttl_seconds_after_
    finished=3600`, xem `jobs.py`) nên log của nó cũng không còn, và hàng
    `ingest_run` là thứ duy nhất còn lại. Bắt buộc ở BIÊN chứ không "nên có"
    trong handler.

    Và `error` bị CẤM khi `status="succeeded"` — không phải sự khắt khe vô cớ:
    cột `ingest_run.error` là thứ giao diện Task 13 hiển thị, và một run thành
    công mang theo một dòng lỗi là một mâu thuẫn mà người đọc phải tự phân xử.
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["succeeded", "failed"]
    error: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _failure_says_why_and_success_does_not(self) -> "IngestCompletionReport":
        if self.status == "failed" and not (self.error or "").strip():
            raise ValueError("a failed run must carry a non-empty error")
        if self.status == "succeeded" and self.error is not None:
            raise ValueError("a succeeded run must not carry an error")
        return self


class LakehouseResolveResponse(BaseModel):
    """`ids[ten]` là `None` cho tên không tồn tại HOẶC chỉ tồn tại ở trạng thái
    khác `active` — endpoint này không phân biệt hai lý do đó với nhau, cùng
    tinh thần `AuthzItemsResponse.roles` ở trên, dù lý do khác: ở đây không có
    gì để rò rỉ cho NGƯỜI DÙNG CUỐI (endpoint không kiểm quyền), nhưng phân
    biệt "chưa từng tồn tại" khỏi "vừa xoá mềm" chỉ để lộ chi tiết vòng đời mà
    người gọi (`loom-query`) không cần biết — nó chỉ cần một id DÙNG ĐƯỢC hay
    không.
    """

    ids: dict[str, uuid.UUID | None]
