"""domain, workspace, item, item_version, role_assignment, audit_log

Revision ID: 0003
Revises: 0002

Sinh bằng `alembic revision --autogenerate` rồi chỉnh tay:

  * `revision`/`down_revision` đổi từ hash sang "0003"/"0002" cho khớp quy ước
    đánh số của repo (0001_initial.py, 0002_user_session_groups.py).
  * Xuống dòng cho vừa 100 cột của ruff. KHÔNG đổi ngữ nghĩa một op nào.

Đã soi từng index và constraint so với models.py — cả `postgresql_where` của
bốn index một phần lẫn `postgresql_nulls_not_distinct` của
uq_role_assignment_principal_scope đều do autogenerate sinh ra đúng (alembic
1.18.5 / SQLAlchemy 2.0.51). Thiếu một trong hai là hỏng ÂM THẦM: mất
`postgresql_where` biến unique-một-phần thành unique thường nên không tạo lại
được tên đã xoá mềm; mất `NULLS NOT DISTINCT` cho hai hàng cấp quyền trùng nhau
cho cùng một nhóm lọt qua. Cả hai giờ có test hành vi trên Postgres thật ở
tests/integration/test_migrations.py, chạy trên schema ĐÃ migrate.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column("id", sa.UUID(), nullable=False),
        # tenant_id CỐ Ý không có FK ở đây và ở item: audit phải sống lâu hơn
        # thứ nó nói về. Xem models.py.
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("actor_user_id", sa.UUID(), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("resource_type", sa.String(length=32), nullable=False),
        sa.Column("resource_id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=True),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("summary", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"], ["app_user.id"], name=op.f("fk_audit_log_actor_user_id_app_user")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
    )
    op.create_index(
        "ix_audit_resource", "audit_log", ["resource_type", "resource_id", "created_at"]
    )
    op.create_index(
        "ix_audit_workspace",
        "audit_log",
        ["workspace_id", "created_at"],
        postgresql_where=sa.text("workspace_id IS NOT NULL"),
    )

    op.create_table(
        "domain",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=False),
        sa.Column("updated_by", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["app_user.id"], name=op.f("fk_domain_created_by_app_user")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name=op.f("fk_domain_tenant_id_tenant"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"], ["app_user.id"], name=op.f("fk_domain_updated_by_app_user")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_domain")),
        sa.UniqueConstraint("tenant_id", "name", name="uq_domain_tenant_name"),
    )
    op.create_index(op.f("ix_domain_tenant_id"), "domain", ["tenant_id"])

    op.create_table(
        "role_assignment",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("principal_type", sa.String(length=8), nullable=False),
        sa.Column("principal_user_id", sa.UUID(), nullable=True),
        sa.Column("principal_group", sa.String(length=255), nullable=True),
        sa.Column("scope_type", sa.String(length=16), nullable=False),
        sa.Column("scope_id", sa.UUID(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # Mạnh hơn `principal_type IN ('user','group')`: buộc principal_type
        # khớp với CỘT nào thật sự có giá trị, nên không thể có hàng cấp quyền
        # cho một NGƯỜI mà tự mô tả là của một NHÓM.
        sa.CheckConstraint(
            "(principal_type = 'user'"
            " AND principal_user_id IS NOT NULL AND principal_group IS NULL)"
            " OR (principal_type = 'group'"
            " AND principal_group IS NOT NULL AND principal_user_id IS NULL)",
            name=op.f("ck_role_assignment_principal_type"),
        ),
        sa.CheckConstraint(
            "role IN ('viewer','contributor','member','admin')",
            name=op.f("ck_role_assignment_role"),
        ),
        sa.CheckConstraint(
            "scope_type IN ('tenant','domain','workspace','item')",
            name=op.f("ck_role_assignment_scope_type"),
        ),
        sa.CheckConstraint(
            "num_nonnulls(principal_user_id, principal_group) = 1",
            name=op.f("ck_role_assignment_one_principal"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["app_user.id"], name=op.f("fk_role_assignment_created_by_app_user")
        ),
        sa.ForeignKeyConstraint(
            ["principal_user_id"],
            ["app_user.id"],
            name=op.f("fk_role_assignment_principal_user_id_app_user"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name=op.f("fk_role_assignment_tenant_id_tenant"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_role_assignment")),
    )
    op.create_index(
        op.f("ix_role_assignment_principal_group"), "role_assignment", ["principal_group"]
    )
    op.create_index(
        op.f("ix_role_assignment_principal_user_id"), "role_assignment", ["principal_user_id"]
    )
    # NULLS NOT DISTINCT: mặc định Postgres coi hai NULL là khác nhau, nên unique
    # thường KHÔNG chặn được hai hàng trùng cho cùng một nhóm.
    op.create_index(
        "uq_role_assignment_principal_scope",
        "role_assignment",
        ["principal_user_id", "principal_group", "scope_type", "scope_id"],
        unique=True,
        postgresql_nulls_not_distinct=True,
    )

    op.create_table(
        "workspace",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("domain_id", sa.UUID(), nullable=True),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("storage_prefix", sa.String(length=255), nullable=False),
        sa.Column(
            "resource_profile",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("state", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=False),
        sa.Column("updated_by", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["app_user.id"], name=op.f("fk_workspace_created_by_app_user")
        ),
        # SET NULL chứ không CASCADE: xoá một domain là thao tác phân loại,
        # không được biến thành mất workspace.
        sa.ForeignKeyConstraint(
            ["domain_id"],
            ["domain.id"],
            name=op.f("fk_workspace_domain_id_domain"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name=op.f("fk_workspace_tenant_id_tenant"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"], ["app_user.id"], name=op.f("fk_workspace_updated_by_app_user")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workspace")),
    )
    op.create_index(op.f("ix_workspace_domain_id"), "workspace", ["domain_id"])
    op.create_index(op.f("ix_workspace_tenant_id"), "workspace", ["tenant_id"])
    # Unique MỘT PHẦN: xoá mềm rồi dùng lại tên phải được. WHERE là phần mang
    # nghĩa — bỏ nó đi thì index vẫn tạo được và vẫn unique, chỉ là sai.
    op.create_index(
        "uq_workspace_active_name",
        "workspace",
        ["tenant_id", "name"],
        unique=True,
        postgresql_where=sa.text("state = 'active'"),
    )

    op.create_table(
        "item",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("folder_path", sa.String(length=1024), server_default="/", nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("definition", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("definition_hash", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("state", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=False),
        sa.Column("updated_by", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["app_user.id"], name=op.f("fk_item_created_by_app_user")
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"], ["app_user.id"], name=op.f("fk_item_updated_by_app_user")
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_item_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_item")),
    )
    # Cả ba cột ASC là CỐ Ý — btree quét NGƯỢC index này cho ra đúng
    # (updated_at DESC, id DESC) khi workspace_id là điều kiện bằng.
    op.create_index(
        "ix_item_pagination",
        "item",
        ["workspace_id", "updated_at", "id"],
        postgresql_where=sa.text("state = 'active'"),
    )
    op.create_index(
        "ix_item_workspace_folder",
        "item",
        ["workspace_id", "folder_path"],
        postgresql_where=sa.text("state = 'active'"),
    )
    op.create_index(op.f("ix_item_workspace_id"), "item", ["workspace_id"])
    op.create_index(
        "uq_item_active_name",
        "item",
        ["workspace_id", "type", "name"],
        unique=True,
        postgresql_where=sa.text("state = 'active'"),
    )

    op.create_table(
        "item_version",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("item_id", sa.UUID(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("definition", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("folder_path", sa.String(length=1024), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("change_note", sa.Text(), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["app_user.id"], name=op.f("fk_item_version_created_by_app_user")
        ),
        sa.ForeignKeyConstraint(
            ["item_id"], ["item.id"], name=op.f("fk_item_version_item_id_item"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_item_version")),
        sa.UniqueConstraint("item_id", "version", name="uq_item_version"),
    )
    op.create_index(op.f("ix_item_version_item_id"), "item_version", ["item_id"])


def downgrade() -> None:
    # Ngược đúng thứ tự tạo. DROP TABLE tự kéo theo index và constraint của
    # bảng đó, nên không cần drop_index rời như autogenerate sinh ra — nhưng
    # THỨ TỰ giữa các bảng thì bắt buộc: item_version → item → workspace →
    # domain, nếu không FK chặn.
    op.drop_table("item_version")
    op.drop_table("item")
    op.drop_table("workspace")
    op.drop_table("role_assignment")
    op.drop_table("domain")
    op.drop_table("audit_log")
