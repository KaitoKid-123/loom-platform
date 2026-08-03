"""tenant, app_user, user_session

Revision ID: 0001
Revises:
"""

import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PgUUID

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def upgrade() -> None:
    op.create_table(
        "tenant",
        sa.Column("id", PgUUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.bulk_insert(
        sa.table(
            "tenant",
            sa.column("id", PgUUID(as_uuid=True)),
            sa.column("name", sa.String),
        ),
        [{"id": DEFAULT_TENANT_ID, "name": "default"}],
    )

    op.create_table(
        "app_user",
        sa.Column("id", PgUUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            PgUUID(as_uuid=True),
            sa.ForeignKey("tenant.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("subject", sa.String(255), nullable=False, unique=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("last_login_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_app_user_tenant_id", "app_user", ["tenant_id"])

    op.create_table(
        "user_session",
        sa.Column("id", PgUUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            PgUUID(as_uuid=True),
            sa.ForeignKey("app_user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("refresh_token", sa.Text()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_user_session_user_id", "user_session", ["user_id"])
    op.create_index("ix_user_session_expires_at", "user_session", ["expires_at"])


def downgrade() -> None:
    op.drop_table("user_session")
    op.drop_table("app_user")
    op.drop_table("tenant")
