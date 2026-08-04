"""user_session.groups

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Cộng thêm và có DEFAULT, nên hàng sẵn có nhận '{}' — không cần backfill và
    # không có khoảng thời gian nào cột này NULL trong khi nó NOT NULL.
    op.add_column(
        "user_session",
        sa.Column(
            "groups",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
    )


def downgrade() -> None:
    op.drop_column("user_session", "groups")
