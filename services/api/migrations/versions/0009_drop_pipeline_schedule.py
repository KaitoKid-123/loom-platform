"""0009_drop_pipeline_schedule

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-17

Bảng `pipeline` (lịch tách riêng, do 0008 tạo) bị BỎ. Lịch chuyển vào
`PipelineDefinition.schedule` bên trong `item.definition` — spec mục 4.

Vì sao bỏ bằng một migration MỚI chứ không sửa lại 0008: 0008 có thể đã chạy ở
nơi khác rồi. Viết lại một migration đã áp dụng thì database đó giữ lại một bảng
`pipeline` mồ côi mà không migration nào còn nhắc tới, và `alembic check` ở
`test_migrations.py` sẽ báo `remove_table` trên chính nó. Một migration DROP thì
đúng ở CẢ HAI trường hợp: database đã chạy 0008 thì bảng bị bỏ ở đây, database
chưa chạy thì nó được tạo rồi bỏ ngay trong cùng một lượt `upgrade head` — vô
hại và không để lại gì.

`downgrade()` phải DỰNG LẠI bảng y hệt 0008, không được để trống:
`test_downgrade_removes_exactly_the_new_tables_and_upgrade_restores_them` hạ
xuống tận 0002, nên `0008.downgrade()` (`DROP TABLE pipeline`) chạy NGAY SAU hàm
này và sẽ hỏng nếu bảng không còn.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
import sqlalchemy.dialects.postgresql as postgresql
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # `if_exists` KHÔNG dùng: bảng phải có mặt vì 0008 vừa tạo nó. Một
    # `DROP ... IF EXISTS` ở đây sẽ nuốt mất bằng chứng rằng chuỗi migration đã
    # lệch khỏi thứ nó tưởng mình đang sửa.
    op.drop_table("pipeline")


def downgrade() -> None:
    # Bản chép NGUYÊN VĂN của `0008.upgrade()` — kể cả `index=True` trên
    # `pipeline_id`/`next_run_at`, vì test hạ-rồi-nâng so cả tập index chứ
    # không chỉ tập bảng.
    op.create_table(
        "pipeline",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenant.id"),
            nullable=False,
        ),
        sa.Column(
            "pipeline_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("item.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
            index=True,
        ),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspace.id"),
            nullable=False,
        ),
        sa.Column("cron", sa.String(64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column(
            "next_run_at",
            sa.DateTime(timezone=True),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "concurrency_cap",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("app_user.id"),
            nullable=False,
        ),
        sa.Column(
            "updated_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("app_user.id"),
            nullable=False,
        ),
    )
