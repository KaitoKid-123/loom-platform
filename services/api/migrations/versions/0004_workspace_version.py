"""Cột `version` cho workspace, để `PATCH /workspaces/{ws}` dùng được `If-Match`.

Spec mục 6 đòi `If-Match` trên `PATCH /workspaces/{ws}`, nhưng bảng `workspace` không
có gì để làm ETag. Ba lựa chọn và lý do chọn cái này:

- `updated_at` làm ETag: hai lần sửa trong cùng micro giây cho ra cùng một ETag, nên
  phép kiểm xung đột im lặng bỏ sót đúng trường hợp nó sinh ra để bắt.
- Bỏ `If-Match` cho workspace: client phải nhớ tài nguyên nào cần header nào, và hai
  người đổi tên cùng lúc thì người sau ghi đè người trước mà không ai biết.
- **Cột `version`** — cùng cơ chế `item` đã dùng và đã được kiểm bằng test đồng thời.
  Một client, một quy tắc.

`server_default='1'` chứ không chỉ `default`: hàng workspace đã tồn tại phải có giá trị
ngay lúc ALTER chạy, còn `default` của SQLAlchemy chỉ áp cho hàng do Python chèn.

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "workspace",
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
    )


def downgrade() -> None:
    op.drop_column("workspace", "version")
