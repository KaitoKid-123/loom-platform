"""ingest_run, stream_state

Revision ID: 0005
Revises: 0004

Sinh bằng `alembic revision --autogenerate` rồi chỉnh tay: chỉ xuống dòng cho
vừa 100 cột của ruff, KHÔNG đổi ngữ nghĩa một op nào — đã soi lại
`UniqueConstraint('lakehouse_id', 'connection_id', 'stream', ...)` trong
`create_table('stream_state', ...)` so với models.py, autogenerate sinh đúng
(alembic 1.18.5 / SQLAlchemy 2.0.51). Nó nằm NGAY TRONG lệnh tạo bảng — không
phải một `ALTER TABLE ADD CONSTRAINT` tách rời — nên autogenerate không in một
dòng log "Detected added constraint" riêng cho nó, dễ khiến người đọc log tưởng
nó bị bỏ sót. Test `test_stream_state_allows_only_one_watermark_per_stream`
xác nhận ràng buộc này tồn tại trên schema ĐÃ MIGRATE, không chỉ trong model.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ingest_run",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("lakehouse_id", sa.UUID(), nullable=False),
        sa.Column("connection_id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("stream", sa.String(length=255), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("rows_written", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["connection_id"], ["item.id"], name=op.f("fk_ingest_run_connection_id_item")
        ),
        sa.ForeignKeyConstraint(
            ["lakehouse_id"], ["item.id"], name=op.f("fk_ingest_run_lakehouse_id_item")
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_ingest_run_workspace_id_workspace"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ingest_run")),
    )
    op.create_index(
        op.f("ix_ingest_run_lakehouse_id"), "ingest_run", ["lakehouse_id"], unique=False
    )
    op.create_table(
        "stream_state",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("lakehouse_id", sa.UUID(), nullable=False),
        sa.Column("connection_id", sa.UUID(), nullable=False),
        sa.Column("stream", sa.String(length=255), nullable=False),
        sa.Column("cursor_column", sa.String(length=255), nullable=False),
        sa.Column("cursor_value", sa.Text(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"], ["item.id"], name=op.f("fk_stream_state_connection_id_item")
        ),
        sa.ForeignKeyConstraint(
            ["lakehouse_id"], ["item.id"], name=op.f("fk_stream_state_lakehouse_id_item")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stream_state")),
        sa.UniqueConstraint(
            "lakehouse_id",
            "connection_id",
            "stream",
            name="uq_stream_state_lakehouse_connection_stream",
        ),
    )
    op.create_index(
        op.f("ix_stream_state_lakehouse_id"), "stream_state", ["lakehouse_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_stream_state_lakehouse_id"), table_name="stream_state")
    op.drop_table("stream_state")
    op.drop_index(op.f("ix_ingest_run_lakehouse_id"), table_name="ingest_run")
    op.drop_table("ingest_run")
