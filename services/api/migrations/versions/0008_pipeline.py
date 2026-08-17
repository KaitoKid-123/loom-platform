"""0008_pipeline

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-17

Pipeline schedule table — stores cron schedule, enabled flag, and next_run_at
for each pipeline item.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
import sqlalchemy.dialects.postgresql as postgresql
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
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
        # Muc nhip tiep theo do cron tinh ra
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


def downgrade() -> None:
    op.drop_table("pipeline")
