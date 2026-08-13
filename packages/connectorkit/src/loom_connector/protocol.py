"""Khung `Connector` — hợp đồng mà mọi nguồn dữ liệu phải tuân theo.

Ba chữ ký giữ nguyên spec v1 §5.6. Ba điều chữ ký KHÔNG tự nói, và cả ba đều là
ràng buộc thật chứ không phải sở thích API:

1. `read` trả `Iterator`, KHÔNG trả `Table`. Cụm có ngân sách RAM, nên không
   connector nào được phép nạp cả bảng vào RAM. Trả `Table` là mời gọi đúng điều
   đó, và lỗi sẽ hiện ra dưới dạng OOMKill ở bảng lớn đầu tiên — xa chỗ gây ra nó.
2. `state` VÀO, không có `state` RA. Watermark mới suy ra từ dữ liệu đã đọc, và
   người báo về control plane là `loom-task`, không phải connector. Connector
   không biết Postgres control plane tồn tại.
3. `check` tách khỏi `read`, để lỗi "không nối được" khác lỗi "đọc hỏng" — hai
   nguyên nhân khác nhau, hai chỗ sửa khác nhau.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol

import pyarrow as pa  # type: ignore[import-untyped]


@dataclass(frozen=True, slots=True)
class CheckResult:
    ok: bool
    message: str


@dataclass(frozen=True, slots=True)
class ColumnSchema:
    name: str
    arrow_type: pa.DataType
    nullable: bool


@dataclass(frozen=True, slots=True)
class StreamSchema:
    name: str
    columns: tuple[ColumnSchema, ...]
    # Cột DÙNG ĐƯỢC làm watermark. Để connector tự nêu thay vì bắt `loom-api`
    # đoán từ kiểu dữ liệu: mỗi nguồn có quy ước riêng, và chỉ connector mới biết
    # cột nào thật sự không-giảm-dần ở nguồn đó.
    candidate_cursors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StreamState:
    """`cursor_value` là CHUỖI, không phải kiểu gốc.

    Nó đi qua JSON tới control plane rồi quay lại, và một timestamp đi vòng qua
    JSON rồi về thì đã mất kiểu. Ép mọi thứ về chuỗi ngay từ đây làm chỗ chuyển
    đổi nằm ở MỘT nơi (connector, chỗ biết kiểu gốc) thay vì rải rác.
    """

    cursor_column: str | None = None
    cursor_value: str | None = None


class Connector(Protocol):
    def check(self) -> CheckResult: ...

    def discover(self) -> list[StreamSchema]: ...

    def read(self, stream: str, state: StreamState) -> Iterator[pa.RecordBatch]: ...
