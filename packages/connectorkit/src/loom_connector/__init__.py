"""Khung `Connector`. Nhận cấu hình, trả `RecordBatch`. **KHÔNG I/O ngoài nguồn.**

Ràng buộc "chỉ nói với nguồn, không chạm gì khác" là lý do package này tách
riêng, và nó KIỂM ĐƯỢC — `tests/test_no_io.py` đọc AST của chính các module ở
đây và bác mọi import ngoài allowlist. Cùng khuôn với `loom_sql` không import
SQLAlchemy.

Không có ràng buộc đó, một connector cụ thể (ví dụ Postgres) sẽ dễ lẻn thêm một
lệnh gọi S3 hay Iceberg "cho tiện", và ranh giới connector/task biến mất.
"""

from loom_connector.protocol import (
    CheckResult,
    ColumnSchema,
    Connector,
    CursorCandidate,
    StreamSchema,
    StreamState,
)

__all__ = [
    "CheckResult",
    "ColumnSchema",
    "Connector",
    "CursorCandidate",
    "StreamSchema",
    "StreamState",
]
