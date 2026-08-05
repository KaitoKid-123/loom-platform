import uuid
from datetime import datetime

from sqlalchemy import (
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
