"""`FakeConnector` — cài đặt thứ hai của `Connector`, tồn tại chỉ để giữ
`tests/test_connector_contract.py` trung thực.

Với đúng một cài đặt (`PostgresConnector`, Task 5), bộ hợp đồng không chứng
minh được điều nó tuyên bố: nó có thể xanh chỉ vì nó tình cờ khớp thói quen
của Postgres, chứ không phải vì nó khớp `Connector`. `FakeConnector` không
chạm Postgres, không chạm mạng, không chạm đĩa — nó giữ toàn bộ dữ liệu trong
RAM bằng `list[dict]` dựng sẵn ở `__init__`. Nếu bộ hợp đồng chỉ mô tả được
thói quen của một nguồn SQL thật, `FakeConnector` là cách rẻ nhất để lộ ra
điều đó, vì hình dạng của nó khác Postgres hoàn toàn.

Một stream duy nhất, tên `"widgets"`, ba cột: `id` (không null, tăng dần),
`updated_at` (không null, tăng dần — cùng kiểu `int64` như `id`, nên bài
cursor `>=` chạy đúng trên cả hai ứng viên bằng một logic ép kiểu duy nhất),
và `payload` (nullable — không cột nào trong Postgres schema thật lại KHÔNG
có ít nhất một cột nullable, nên cố tình có mặt ở đây dù không bài nào trong
bộ hợp đồng kiểm tra null trực tiếp).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import cast

import pyarrow as pa  # type: ignore[import-untyped]

from loom_connector.protocol import (
    CheckResult,
    ColumnSchema,
    CursorCandidate,
    StreamSchema,
    StreamState,
)

_STREAM_NAME = "widgets"

_SCHEMA = StreamSchema(
    name=_STREAM_NAME,
    columns=(
        ColumnSchema(name="id", arrow_type=pa.int64(), nullable=False),
        ColumnSchema(name="updated_at", arrow_type=pa.int64(), nullable=False),
        ColumnSchema(name="payload", arrow_type=pa.string(), nullable=True),
    ),
    # Cả hai cùng ứng viên: `id` tăng dần vì nó là thứ tự chèn, `updated_at`
    # tăng dần vì đây là fake dựng sẵn (không mô phỏng cập nhật ngoài thứ tự).
    #
    # `cursor_type="bigint"` chứ không một chuỗi tuỳ ý: nó phải nằm trong
    # `CURSOR_TYPE_ALLOWLIST` (`loom-api` từ chối mọi kiểu ngoài đó) và phải nói
    # ĐÚNG về dữ liệu fake này — cả hai cột là `pa.int64()`, mà `bigint` là tên
    # Postgres của int64. Khai `integer` ở đây sẽ là một lời nói dối nhỏ mà
    # `loom-task` không có cách nào phát hiện, và nó sẽ đi thẳng vào
    # `stream_state.cursor_type` của một lần nạp thật.
    candidate_cursors=(
        CursorCandidate(name="id", cursor_type="bigint"),
        CursorCandidate(name="updated_at", cursor_type="bigint"),
    ),
)


class FakeConnector:
    """`n_rows` dòng dựng sẵn lúc khởi tạo, `batch_size` dòng mỗi `RecordBatch`.

    `batch_size` nhỏ hơn `n_rows` là ĐIỀU KIỆN để bài "lazy iterator" trong bộ
    hợp đồng có ý nghĩa: nếu chỉ có một batch, "lấy một rồi dừng" và "lấy hết"
    là cùng một hành động, và phép kiểm không phân biệt được hai cách cài đặt.
    """

    def __init__(self, n_rows: int, batch_size: int = 10) -> None:
        if n_rows <= 0:
            raise ValueError("n_rows phải dương — một fake rỗng không đọc được gì")
        if batch_size <= 0:
            raise ValueError("batch_size phải dương")
        self._batch_size = batch_size
        # id và updated_at cùng tăng dần theo i — đơn giản có chủ đích: bộ
        # hợp đồng không cần watermark thực tế phức tạp hơn "tăng dần".
        self._rows: list[dict[str, object]] = [
            {
                "id": i,
                "updated_at": i,
                "payload": None if i % 5 == 0 else f"payload-{i}",
            }
            for i in range(n_rows)
        ]

    def check(self) -> CheckResult:
        return CheckResult(ok=True, message=f"fake sẵn sàng với {len(self._rows)} dòng in-memory")

    def discover(self) -> list[StreamSchema]:
        return [_SCHEMA]

    def read(self, stream: str, state: StreamState) -> Iterator[pa.RecordBatch]:
        if stream != _STREAM_NAME:
            raise ValueError(f"không có stream '{stream}' — fake chỉ có '{_STREAM_NAME}'")
        return self._read(state)

    def _read(self, state: StreamState) -> Iterator[pa.RecordBatch]:
        """Hàm generator riêng: `read()` ném lỗi NGAY khi gọi (bài 7), một
        generator thì hoãn mọi thân hàm tới lần `next()` đầu tiên — nếu validate
        tên stream nằm trong hàm generator, `read("stream-la", state)` sẽ trả về
        một iterator "hợp lệ" và chỉ nổ khi người gọi bắt đầu lặp, không phải
        khi gọi `read()`. Tách hàm là cách duy nhất có validate-sớm CỘNG
        VỚI thân đọc vẫn lazy."""
        rows = self._rows
        if state.cursor_column is not None:
            column = state.cursor_column
            # So sánh bằng CHUỖI vì StreamState.cursor_value luôn là chuỗi (xem
            # docstring StreamState) — ép kiểu gốc (int ở đây) lại để so đúng
            # thứ tự số, không phải thứ tự từ điển ("10" < "9" theo chuỗi).
            boundary = int(state.cursor_value) if state.cursor_value is not None else None
            if boundary is not None:
                # `>=`, KHÔNG `>` — xem docstring test_cursor_filter_is_inclusive_not_exclusive
                # để biết vì sao lệch một ký tự ở đây là mất dữ liệu thật.
                # `cast`: `_rows` khai `object` cho giá trị (cột `payload` là
                # `str | None`), nhưng `id`/`updated_at` luôn là `int` do chính
                # `__init__` dựng ra — mypy không suy được điều đó qua `dict`.
                rows = [r for r in rows if cast(int, r[column]) >= boundary]

        for start in range(0, len(rows), self._batch_size):
            chunk = rows[start : start + self._batch_size]
            yield pa.RecordBatch.from_pylist(chunk)
