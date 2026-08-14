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
class CursorCandidate:
    """Một cột DÙNG ĐƯỢC làm watermark, và KIỂU mà phép so sánh phải đọc nó dưới.

    **Hai trường đi cùng nhau vì chúng vô dụng khi tách rời.** Đường báo tiến độ
    (`IngestProgressReport`) đòi `cursor_column` VÀ `cursor_type` cùng lúc —
    thiếu kiểu thì `loom-api` chỉ so được CHUỖI, và so chuỗi trên một cursor
    `bigint` làm watermark kẹt vĩnh viễn ở lần đầu vượt mốc đổi số chữ số (xem
    `loom_core.cursor`). Nên một `candidate_cursors` chỉ mang TÊN buộc người gọi
    phải tìm kiểu ở đâu đó khác, và chỗ "đâu đó khác" duy nhất còn lại là suy
    ngược từ `ColumnSchema.arrow_type` — tức là dựng một bản ánh xạ NGƯỢC của
    `_ARROW_TYPE_MAP` (`postgres.py`) và giữ hai bảng khớp nhau bằng trí nhớ.
    Bản ánh xạ đó còn không phải song ánh: `text`, `numeric`, `uuid`, `json` đều
    về `pa.string()`, nên chiều ngược không xác định.

    `cursor_type` là chuỗi kiểu của NGUỒN, khớp chính xác `CURSOR_TYPE_ALLOWLIST`
    ở `loom_core.cursor` (giá trị `information_schema.columns.data_type` của
    Postgres). Connector đã đọc chuỗi đó để LỌC ra danh sách này, nên nó có sẵn
    trong tay — bản trước ném nó đi, rồi `loom-task` phải đoán lại.
    `test_candidate_cursors_are_real_columns` canh cả hai trường.
    """

    name: str
    cursor_type: str


@dataclass(frozen=True, slots=True)
class StreamSchema:
    name: str
    columns: tuple[ColumnSchema, ...]
    # Cột DÙNG ĐƯỢC làm watermark, KÈM kiểu nguồn của nó (xem `CursorCandidate`).
    # Để connector tự nêu thay vì bắt `loom-api` đoán từ kiểu dữ liệu: mỗi nguồn
    # có quy ước riêng, và chỉ connector mới biết cột nào thật sự không-giảm-dần
    # ở nguồn đó.
    candidate_cursors: tuple[CursorCandidate, ...]


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
