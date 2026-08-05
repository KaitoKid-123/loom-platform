"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
## Không có dòng này thì autogenerate render `postgresql.JSONB(...)` mà KHÔNG
## kèm `from sqlalchemy.dialects import postgresql`, và migration sinh ra chết
## bằng NameError ngay lúc apply. Template mặc định của alembic có `${imports}`;
## bản chép tay trong repo đã đánh rơi nó, và không ai thấy vì 0001/0002 đều
## viết tay. Đã dính đúng một lần ở migration 0003.
% if imports:
${imports}
% endif

revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
