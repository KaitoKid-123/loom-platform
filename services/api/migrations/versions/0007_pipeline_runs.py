"""0007_pipeline_runs

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-16

Pipeline scheduling: two tables for tracking pipeline runs and their steps.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
import sqlalchemy.dialects.postgresql as postgresql
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Pipeline run — one row per scheduled tick
    op.create_table(
        "pipeline_run",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "pipeline_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("item.id"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspace.id"),
            nullable=False,
        ),
        # `scheduled_for` la MOC NHIP, khong phai luc chay
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "status", sa.String(16), nullable=False, server_default="pending"
        ),
        sa.Column("skip_reason", sa.Text()),
        sa.Column(
            "run_as_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("app_user.id"),
            nullable=False,
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("error", sa.Text()),
        sa.UniqueConstraint(
            "pipeline_id",
            "scheduled_for",
            name="uq_pipeline_run_pipeline_scheduled_for",
        ),
    )

    # Pipeline step run — one row per step in a pipeline run
    op.create_table(
        "pipeline_step_run",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "pipeline_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("pipeline_run.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("step_index", sa.Integer(), nullable=False),
        sa.Column("step_type", sa.String(16), nullable=False),
        sa.Column(
            "status", sa.String(16), nullable=False, server_default="pending"
        ),
        # Buoc nap NOI vao bang 3a, khong chep trang thai sang day
        sa.Column(
            "ingest_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ingest_run.id"),
        ),
        sa.Column("query_id", sa.Text()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("error", sa.Text()),
        sa.UniqueConstraint(
            "pipeline_run_id",
            "step_index",
            name="uq_pipeline_step_run_run_index",
        ),
    )

    # Rewrite old pipeline definitions (nodes/edges -> steps)
    op.execute(
        """
        UPDATE item
        SET definition = jsonb_build_object('schema_version', 1, 'steps', '[]'::jsonb)
        WHERE type = 'pipeline'
          AND (definition ? 'nodes' OR definition ? 'edges')
        """
    )


def downgrade() -> None:
    # Restore old pipeline definitions
    op.execute(
        """
        UPDATE item
        SET definition = jsonb_build_object(
            'schema_version', 1, 'nodes', '[]'::jsonb, 'edges', '[]'::jsonb
        )
        WHERE type = 'pipeline'
          AND definition ? 'steps'
        """
    )

    op.drop_table("pipeline_step_run")
    op.drop_table("pipeline_run")
