"""stream_state.cursor_type

Revision ID: 0006
Revises: 0005

Migration RIÊNG chứ không sửa 0005: 0005 đã commit, và một migration đã ra khỏi
nhánh là bất biến — sửa nó nghĩa là mọi database đã chạy nó vẫn ở lại schema cũ
trong khi `alembic_version` bảo rằng chúng đã cập nhật.

**NULLABLE, và đó là lựa chọn chứ không phải sự lười.** Không có giá trị mặc
định nào ĐÚNG cho những hàng đã có: kiểu của một watermark cũ không suy ra được
từ bất cứ cột nào trong bảng, nên điền bừa `'bigint'` sẽ làm lần báo tiến độ
sau đó so sánh một chuỗi ngày tháng như một số nguyên. `ADD COLUMN ... NOT NULL`
không kèm mặc định thì lại làm `alembic upgrade head` HỎNG trên mọi database có
sẵn dù chỉ một hàng. Null = "không biết kiểu", và
`routers/internal_ingest.py` xử lý nó bằng cách ĐẶT LẠI watermark thay vì so
sánh — cùng cách nó xử lý một `cursor_column` đã đổi.

`String(64)`: giá trị dài nhất trong `loom_core.cursor.CURSOR_TYPE_ALLOWLIST` là
`'timestamp without time zone'` (27 ký tự). 64 là chỗ thở cho một tên kiểu dài
hơn ở nguồn thứ hai, không phải một con số lấy từ hư không.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("stream_state", sa.Column("cursor_type", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("stream_state", "cursor_type")
