import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Triển khai nội bộ chỉ có một tenant, nhưng cột tenant_id có mặt từ đầu
# để sau này không phải migrate đau (xem spec mục 4.1).
DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Tenant(Base):
    __tablename__ = "tenant"

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AppUser(Base):
    __tablename__ = "app_user"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("tenant.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    subject: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UserSession(Base):
    __tablename__ = "user_session"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("app_user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    refresh_token: Mapped[str | None] = mapped_column(Text)
    # Nhóm được CHỤP lúc đăng nhập, không đọc lại token mỗi request. Đánh đổi:
    # đổi nhóm ở IdP chỉ có hiệu lực ở lần đăng nhập sau. Có chủ đích — xem spec
    # mục 3.3 — và phải nằm trong tài liệu vận hành, nếu không người quản trị
    # ngồi đợi quyền tự đổi.
    groups: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default="{}")
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


ACTIVE = "active"
DELETED = "deleted"


class TimestampMixin:
    """created_by/updated_by là NOT NULL có chủ đích: mọi hàng phải truy được về
    một người. Audit trả lời 'ai đổi gì'; hai cột này trả lời 'ai tạo ra thứ này'
    mà không phải quét audit."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Domain(Base, TimestampMixin):
    __tablename__ = "domain"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_domain_tenant_name"),)

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("tenant.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("app_user.id"), nullable=False
    )
    updated_by: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("app_user.id"), nullable=False
    )


class Workspace(Base, TimestampMixin):
    __tablename__ = "workspace"
    __table_args__ = (
        # Unique MỘT PHẦN, không phải unique thường. Xem test_models_workspace.
        Index(
            "uq_workspace_active_name",
            "tenant_id",
            "name",
            unique=True,
            postgresql_where=text("state = 'active'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("tenant.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # SET NULL chứ không CASCADE: xoá một domain là thao tác phân loại, không
    # được biến thành mất workspace.
    domain_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("domain.id", ondelete="SET NULL"), index=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    # Giai đoạn 2 dùng. NOT NULL từ giờ để Giai đoạn 2 không phải backfill.
    storage_prefix: Mapped[str] = mapped_column(String(255), nullable=False)
    # ETag của workspace, cùng cơ chế `item.version`. Xem migration 0004 để biết vì sao
    # không dùng `updated_at`.
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    resource_profile: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default=ACTIVE)
    created_by: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("app_user.id"), nullable=False
    )
    updated_by: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("app_user.id"), nullable=False
    )


class Item(Base, TimestampMixin):
    __tablename__ = "item"
    __table_args__ = (
        Index(
            "uq_item_active_name",
            "workspace_id",
            "type",
            "name",
            unique=True,
            postgresql_where=text("state = 'active'"),
        ),
        Index(
            "ix_item_workspace_folder",
            "workspace_id",
            "folder_path",
            postgresql_where=text("state = 'active'"),
        ),
        # Thứ tự cột PHẢI khớp ORDER BY của cursor: (updated_at DESC, id DESC).
        # Cả ba cột ASC là CỐ Ý — btree quét NGƯỢC index này cho ra đúng thứ tự
        # đó khi workspace_id là điều kiện bằng. Đặt `updated_at DESC` như DDL
        # trong spec mục 3.1 thì không chiều quét nào khớp và planner phải Sort.
        Index(
            "ix_item_pagination",
            "workspace_id",
            "updated_at",
            "id",
            postgresql_where=text("state = 'active'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    folder_path: Mapped[str] = mapped_column(String(1024), nullable=False, server_default="/")
    description: Mapped[str | None] = mapped_column(Text)
    definition: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    # Dấu vết NỘI DUNG của definition — cho Git drift ở Giai đoạn 5. KHÔNG phải
    # ETag: nó không phủ display_name/folder_path nên không phát hiện được xung
    # đột đổi tên. ETag là `version`.
    definition_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default=ACTIVE)
    created_by: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("app_user.id"), nullable=False
    )
    updated_by: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("app_user.id"), nullable=False
    )


class ItemVersion(Base):
    __tablename__ = "item_version"
    __table_args__ = (UniqueConstraint("item_id", "version", name="uq_item_version"),)

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    item_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("item.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    definition: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    # Lưu cả metadata, không chỉ definition: restore phải hoàn tác được một lần
    # đổi tên hoặc di chuyển folder, không riêng nội dung.
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    folder_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    change_note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("app_user.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class RoleAssignment(Base):
    __tablename__ = "role_assignment"
    __table_args__ = (
        # `name` CHỈ là phần đuôi: NAMING_CONVENTION["ck"] đã có sẵn tiền tố
        # "ck_%(table_name)s_". Truyền tên đầy đủ vào đây thì nó bị ghép hai lần
        # và constraint mang tên ck_role_assignment_ck_role_assignment_...
        #
        # Mạnh hơn `principal_type IN ('user','group')`: buộc principal_type
        # khớp với CỘT nào thật sự có giá trị. Không có vế này thì
        # principal_type='group' cùng với principal_user_id vẫn hợp lệ. Đó
        # KHÔNG phải lỗ hổng phân quyền — effective_role khớp thẳng theo cột,
        # không đọc principal_type — nhưng Giai đoạn 1b trả principal_type ra
        # trong danh sách quyền, nên giao diện sẽ mô tả một grant là "của nhóm"
        # trong khi nó cấp cho một người. Đóng lúc bảng còn rỗng thì miễn phí;
        # đóng sau thì phải dọn dữ liệu trước.
        CheckConstraint(
            "(principal_type = 'user'"
            " AND principal_user_id IS NOT NULL AND principal_group IS NULL)"
            " OR (principal_type = 'group'"
            " AND principal_group IS NOT NULL AND principal_user_id IS NULL)",
            name="principal_type",
        ),
        CheckConstraint(
            "scope_type IN ('tenant','domain','workspace','item')",
            name="scope_type",
        ),
        CheckConstraint(
            "role IN ('viewer','contributor','member','admin')",
            name="role",
        ),
        # Đúng MỘT trong hai. Cả hai NULL thì hàng không thuộc về ai; cả hai có
        # giá trị thì không rõ hàng nói về ai.
        CheckConstraint(
            "num_nonnulls(principal_user_id, principal_group) = 1",
            name="one_principal",
        ),
        # NULLS NOT DISTINCT: mặc định Postgres coi hai NULL là khác nhau, nên
        # unique thường KHÔNG chặn được hai hàng trùng cho cùng một nhóm (cả hai
        # có principal_user_id = NULL).
        Index(
            "uq_role_assignment_principal_scope",
            "principal_user_id",
            "principal_group",
            "scope_type",
            "scope_id",
            unique=True,
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    principal_type: Mapped[str] = mapped_column(String(8), nullable=False)
    principal_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("app_user.id", ondelete="CASCADE"), index=True
    )
    principal_group: Mapped[str | None] = mapped_column(String(255), index=True)
    scope_type: Mapped[str] = mapped_column(String(16), nullable=False)
    # KHÔNG có FK — trỏ tới bốn bảng tuỳ scope_type. Xem spec mục 3.1.
    scope_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("app_user.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_resource", "resource_type", "resource_id", "created_at"),
        Index(
            "ix_audit_workspace",
            "workspace_id",
            "created_at",
            postgresql_where=text("workspace_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    actor_user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("app_user.id"), nullable=False
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    # Nối với log/trace của Giai đoạn 0. NOT NULL: một dòng audit không truy được
    # về request nào thì mất nửa giá trị.
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    summary: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class IngestRun(Base):
    """Một hàng cho mỗi lần thử nạp một stream nguồn vào bronze.

    Trạng thái: `pending`, `running`, `succeeded`, `failed`. Ban đầu KHÔNG có
    `cancelled` — chưa có gì tự sinh run nên chưa có gì để huỷ. Với Giai đoạn
    3d (pipeline scheduling), `cancelled` trở nên có nghĩa: một pipeline run có
    thể chủ động huỷ ingest run đang chạy trước khi nó xong. Một nút Huỷ dựng
    nửa vời vẫn tệ hơn không có, nên huỷ chỉ được thực hiện khi Giai đoạn 3d
    đã đủ để thực sự dừng được pod đang chạy.

    `pending` là khoảng trống giữa "hàng vừa được tạo" và "Job đã báo đang
    chạy". Một run kẹt mãi ở `pending` nghĩa là Job chưa bao giờ khởi động
    được — thường do sai tên Secret — và vòng reconcile lười của Task 13 PHẢI
    chuyển nó thành `failed`, không được để nó nằm mãi ở đây.

    **`running` nghĩa là "pod ĐÃ LẤY spec ít nhất một lần", KHÔNG phải "pod còn
    sống".** Task 10 đặt chuyển tiếp `pending -> running` ở `GET
    /internal/ingest/{run_id}/spec` (xem `routers/internal_ingest.py`), và đó là
    tín hiệu DUY NHẤT trong bảng này về việc pod đã chạy. Hệ quả mà Task 13 phải
    xử lý: một pod bị OOMKill (hoặc node chết, hoặc pod bị xoá) SAU khi lấy spec
    nằm ở `running` VĨNH VIỄN — không có heartbeat, và `/complete` sẽ không bao
    giờ tới. Vòng reconcile vì vậy KHÔNG được tin cột này một mình; nó phải đối
    chiếu với `JobLauncher.status(run_id)` (`jobs.py`), thứ trả về
    `exists`/`active`/`succeeded`/`failed` đọc từ chính Kubernetes. Điều đó khả
    thi mà không cần thêm cột nào: `job_name` tất định theo `run_id`, nên trạng
    thái Job của một run luôn tra được. "`running` + Job không còn tồn tại" là
    một run đã chết và phải thành `failed`.

    **KHOẢNG TRỐNG ĐÃ BIẾT, ghi tên chứ không xây ở 3a: `start_ingest` không
    chống trùng.** Không gì ngăn hai hàng `ingest_run` cùng sống cho cùng
    `(lakehouse_id, connection_id, stream)`, và `job_name` tất định theo
    `run_id` chứ không theo stream — nên hai lần bấm Nạp cho ra HAI Job cùng
    đọc nguồn và cùng ghi vào một bảng bronze. `_advance_watermark` đã được làm
    an toàn với đua nên không ai 500 và watermark không lùi (xem docstring của
    nó), nhưng đó chỉ chữa phần watermark: CÔNG VIỆC vẫn bị làm hai lần và dữ
    liệu trùng vẫn vào bronze bất kể ai thắng. Cách chữa đúng chỗ là một 409 ở
    `start_ingest` khi stream đó đã có run `pending`/`running` — giá trị cao
    hơn hẳn cái khoá, và nó thuộc đường TẠO run, không thuộc đường callback.

    Pod nạp KHÔNG có credential Postgres nào: nó chỉ lấy spec và báo tiến độ
    qua `/internal/ingest/*` (khuôn shared-secret đã dùng ở Giai đoạn 2b), nên
    bảng này chỉ được `loom-api` đọc và ghi — pod không bao giờ đụng tới nó
    trực tiếp.
    """

    __tablename__ = "ingest_run"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    lakehouse_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("item.id"), nullable=False, index=True
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("item.id"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("workspace.id"), nullable=False
    )
    # `schema.table` phía nguồn — xem StreamSchema.name ở loom_connector.protocol.
    stream: Mapped[str] = mapped_column(String(255), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    rows_written: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class StreamState(Base):
    """Watermark của một stream — đúng MỘT hàng cho mỗi (lakehouse, connection, stream).

    `cursor_value` là chuỗi, không phải cột có kiểu gốc — khớp `StreamState` ở
    `loom_connector.protocol`: giá trị đi qua JSON tới pod nạp rồi quay lại, và
    một timestamp đi vòng qua JSON thì đã mất kiểu. Ép về chuỗi ngay từ đây làm
    điểm chuyển đổi duy nhất nằm ở connector — nơi biết kiểu gốc — thay vì rải
    rác khắp nơi.

    `cursor_type` (migration 0006) là thứ TRẢ LẠI kiểu đã mất đó, và nó không
    phải siêu dữ liệu trang trí: không có nó, phép so "watermark chỉ được tiến"
    chỉ so được CHUỖI, và trên một cursor `bigint` — trường hợp thường gặp
    nhất — chuỗi làm watermark kẹt vĩnh viễn ở lần đầu vượt mốc đổi số chữ số
    (`"1000" > "400"` là `False`), sinh trùng lặp không giới hạn mà không lỗi
    nào báo ra. Xem `loom_core.cursor`.

    `cursor_type` NULL ĐƯỢC, và null nghĩa là "hàng có từ trước 0006, không
    biết kiểu". Nó không có mặc định đúng nào để điền: kiểu của một watermark
    cũ không suy ra được từ bất cứ đâu trong bảng. Đường báo tiến độ vì vậy coi
    null như một `cursor_type` KHÁC và ĐẶT LẠI watermark thay vì so sánh — cùng
    lập luận với `cursor_column` đổi, ngay dưới đây.

    UNIQUE trên (lakehouse_id, connection_id, stream) — CỐ Ý KHÔNG có
    cursor_column trong khoá. Cho phép hai hàng tồn tại nghĩa là đổi
    cursor_column sẽ để lại một hàng cũ mà lần nạp sau chọn bừa, và giá trị nó
    mang là một con số thuộc về một thang đo khác hẳn — bỏ sót dữ liệu mà
    không có lỗi nào báo ra.

    """

    __tablename__ = "stream_state"
    __table_args__ = (
        UniqueConstraint(
            "lakehouse_id",
            "connection_id",
            "stream",
            name="uq_stream_state_lakehouse_connection_stream",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    lakehouse_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("item.id"), nullable=False, index=True
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("item.id"), nullable=False
    )
    stream: Mapped[str] = mapped_column(String(255), nullable=False)
    cursor_column: Mapped[str] = mapped_column(String(255), nullable=False)
    # Một trong `loom_core.cursor.CURSOR_TYPE_ALLOWLIST`, hoặc NULL cho hàng có
    # từ trước migration 0006 — xem docstring lớp.
    cursor_type: Mapped[str | None] = mapped_column(String(64))
    cursor_value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Pipeline(Base):
    """Lich chay cho mot pipeline item.

    `next_run_at` la moc nhip tiep theo do cron tinh ra, khong phai luc
    thuc su chay. Tick endpoint so sanh `next_run_at <= now` de quyet dinh
    lich nao can xu ly trong tick hien tai.
    """

    __tablename__ = "pipeline"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False
    )
    pipeline_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("item.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("workspace.id"),
        nullable=False,
    )
    cron: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(default=True, nullable=False)
    # Muc nhip tiep theo do cron tinh ra
    next_run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    # Gioi han so run dong thoi cho pipeline nay
    concurrency_cap: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_by: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("app_user.id"), nullable=False
    )
    updated_by: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("app_user.id"), nullable=False
    )


class PipelineRun(Base):
    """Mot hang cho moi lan chay duoc lap lich mot pipeline.

    `scheduled_for` la MOC NHIP cua scheduler, khong phai luc chay that su.
    Mot run co the bi skip (neu tat ca cac step deu bi skip) hoac bi loi o bat
    ky buoc nao.

    Trang thai: `pending`, `running`, `succeeded`, `failed`, `skipped`.
    """

    __tablename__ = "pipeline_run"
    __table_args__ = (
        UniqueConstraint(
            "pipeline_id",
            "scheduled_for",
            name="uq_pipeline_run_pipeline_scheduled_for",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    pipeline_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("item.id"), nullable=False, index=True
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("workspace.id"), nullable=False
    )
    # MOC NHIP chu khong phai luc chay
    scheduled_for: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="pending"
    )
    # Ly do skip, neu pipeline duoc lap lich nhung khong co gi de chay
    skip_reason: Mapped[str | None] = mapped_column(Text)
    run_as_user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("app_user.id"), nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)


class PipelineStepRun(Base):
    """Mot hang cho moi buoc trong mot pipeline run.

    Cac buoc ingest NOI vao bang `ingest_run` (bang 3a), khong chep trang thai
    sang day. Bang nay chi theo doi trang thai cua pipeline step thuan, con
    trang thai ingest duoc doc truc tiep tu bang ingest_run qua FK.

    Trang thai: `pending`, `running`, `succeeded`, `failed`.
    """

    __tablename__ = "pipeline_step_run"
    __table_args__ = (
        UniqueConstraint(
            "pipeline_run_id",
            "step_index",
            name="uq_pipeline_step_run_run_index",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    pipeline_run_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("pipeline_run.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    step_type: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="pending"
    )
    # FK toi ingest_run de theo doi trang thai buoc nap — KHONG chep trang
    # thai sang day
    ingest_run_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("ingest_run.id")
    )
    # query_id de truy van trang thai transform
    query_id: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
