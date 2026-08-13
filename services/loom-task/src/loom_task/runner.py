"""Vòng lặp nạp `incremental`: GHI TRƯỚC, BÁO SAU. Xem spec 3a mục 3.1.

`Sink` là một Protocol chứ không phải `loom_iceberg.Lakehouse` trực tiếp: nó cho
phép kiểm THỨ TỰ và khả năng nạp lại mà không cần Iceberg, và chính thứ tự mới
là chỗ lỗi mất-dòng sinh ra. Đường ghi Iceberg thật được kiểm ở integration.

Chỉ có `run_incremental` ở đây. Đường `full` (bảng tạm rồi tráo tên — thiết kế
"một commit ở cuối" đã bị ĐO 2 bác bỏ, xem spec mục 3.1) là việc của Task 12, và
`Sink` cố ý CHƯA có `stage`/`promote`/`drop`: khai sẵn một phương thức mà không
đường nào gọi và không test nào canh là mời một chữ ký sai đứng đó cho tới ngày
có người tin nó.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Protocol

import pyarrow as pa  # type: ignore[import-untyped]

from loom_connector import Connector, CursorCandidate, StreamSchema
from loom_core.cursor import format_cursor_value
from loom_task.client import IngestClientLike

# Ba cột metadata của bronze (spec v1 mục 5.5). `check_schema` ở Task 14 phải
# loại trừ đúng ba tên này, nếu không MỌI lần nạp thứ hai đều báo schema drift.
BRONZE_COLUMNS = ("_ingested_at", "_source", "_batch_id")


class Boom(RuntimeError):
    """Chỉ dùng để kiểm nạp lại. KHÔNG bắt được ở mã sản xuất, và không có đường
    nào ném nó ngoài `crash_after_batch` — một tham số mà `main.py` không truyền."""


class StreamNotDiscovered(RuntimeError):
    """Nguồn không có stream mà run này nói tên.

    Tách khỏi `CursorNotAvailable` vì hai lỗi này sửa ở hai chỗ khác nhau: tên
    stream sai là sửa ở cấu hình run (hoặc bảng đã bị xoá ở nguồn), còn thiếu
    cursor dùng được là chọn nhầm mode — `full` chạy được trên một bảng không có
    cột nào không-giảm-dần, `incremental` thì không.
    """


class CursorNotAvailable(RuntimeError):
    """Không có cột nào của stream này dùng được làm watermark, hoặc cột mà
    watermark đang đứng trên không còn dùng được ở nguồn."""


class Sink(Protocol):
    def append(self, batch: pa.RecordBatch) -> None:
        """Ghi VÀ commit ngay — hợp đồng của `incremental`.

        Một `append` chỉ ghi file dữ liệu mà chưa commit sẽ phá đúng tính chất
        mà `run_incremental` dựa vào: sau khi nó trả về, dữ liệu của lô này phải
        ĐÃ bền, vì watermark được báo ngay sau đó.
        """


def bronze_table_name(connection_slug: str, stream: str) -> str:
    """`bronze.<slug connection>__<schema nguồn>_<bảng nguồn>`.

    HAI dấu gạch dưới ngăn phần connection với phần bảng, để hai nguồn khác nhau
    có bảng trùng tên không đụng nhau, và để đọc ngược ra được nguồn từ tên bảng.
    Một dấu gạch thôi thì `pos_public_orders` không phân biệt được với connection
    tên `pos_public` đọc bảng `orders`.
    """
    schema, _, table = stream.partition(".")
    if not schema or not table:
        raise ValueError(f"stream phải là 'schema.table', nhận {stream!r}")
    return f"bronze.{connection_slug}__{schema}_{table}"


def add_bronze_columns(batch: pa.RecordBatch, source: str, batch_id: uuid.UUID) -> pa.RecordBatch:
    """Thêm đúng ba cột `BRONZE_COLUMNS` vào cuối lô.

    `_ingested_at` lấy đồng hồ của tiến trình này MỘT lần cho cả lô: một dấu thời
    gian cho mỗi DÒNG sẽ nói rằng các dòng trong cùng một lô hạ cánh ở những thời
    điểm khác nhau, điều không đúng — chúng vào bằng một commit.
    """
    rows = batch.num_rows
    now = datetime.now(UTC)
    return pa.RecordBatch.from_arrays(
        [
            *batch.columns,
            pa.array([now] * rows, type=pa.timestamp("us", tz="UTC")),
            pa.array([source] * rows, type=pa.string()),
            pa.array([str(batch_id)] * rows, type=pa.string()),
        ],
        names=[*batch.schema.names, *BRONZE_COLUMNS],
    )


def resolve_cursor(
    streams: Sequence[StreamSchema], stream: str, remembered_column: str | None
) -> CursorCandidate:
    """Cột watermark và KIỂU của nó, lấy từ CONNECTOR — không đoán ở đâu khác.

    **Vì sao hàm này tồn tại.** `IngestSpec.cursor_type` là `None` cho tới khi có
    watermark, nên ở lần nạp `incremental` ĐẦU TIÊN của một stream, control plane
    không có kiểu để đưa. `IngestProgressReport` thì đòi `cursor_type` ngay từ lô
    đầu. Chỗ duy nhất biết kiểu THẬT của cột ở nguồn là connector — nó đọc
    `information_schema.columns.data_type` để lọc `candidate_cursors` — nên kiểu
    đi ra từ `discover()` (xem `CursorCandidate`), không phải từ một bảng ánh xạ
    ngược `arrow_type -> kiểu Postgres` dựng thêm ở đây.

    **`remembered_column` (tức `IngestSpec.cursor_column`) THẮNG khi có.** Đổi
    cột giữa hai lần chạy làm `loom-api` ĐẶT LẠI watermark thay vì so sánh (hai
    thang đo khác nhau — xem `_advance_watermark`), tức là đọc lại từ đầu. Nên
    một khi đã chọn, lựa chọn đó phải giữ nguyên, và nơi nó được nhớ là hàng
    `stream_state`.

    **Lần đầu thì lấy ứng viên ĐẦU TIÊN, và đó là một lựa chọn của mã này, không
    phải của người dùng.** Giai đoạn 3a chưa có ô nào để chọn cột cursor (3c mới
    có UI), nên phải có ai đó chọn; thứ tự của `candidate_cursors` là
    `ordinal_position` ở nguồn. Nói ra để không ai đọc nó thành "cột tốt nhất".
    Điều làm lựa chọn này an toàn được là nó chỉ xảy ra MỘT lần cho mỗi stream:
    lô đầu báo cột đó về, và từ đó `remembered_column` quyết định.

    Cột đã nhớ mà không còn trong `candidate_cursors` thì NÉM, không rơi về ứng
    viên khác: kiểu ở nguồn vừa đổi (một `ALTER TABLE ... TYPE text`) nghĩa là
    watermark cũ có thể không còn đọc được dưới kiểu mới, và tự chọn một cột khác
    sẽ lặng lẽ nạp lại toàn bộ bảng dưới cái tên "incremental".
    """
    schema = next((s for s in streams if s.name == stream), None)
    if schema is None:
        raise StreamNotDiscovered(
            f"nguồn không có stream {stream!r} — discover() thấy {sorted(s.name for s in streams)}"
        )
    if not schema.candidate_cursors:
        raise CursorNotAvailable(
            f"stream {stream!r} không có cột nào dùng được làm watermark "
            "(kiểu phải nằm trong CURSOR_TYPE_ALLOWLIST) — mode 'incremental' "
            "không chạy được trên nó, 'full' thì được"
        )
    if remembered_column is None:
        return schema.candidate_cursors[0]

    remembered = next((c for c in schema.candidate_cursors if c.name == remembered_column), None)
    if remembered is None:
        raise CursorNotAvailable(
            f"watermark của stream {stream!r} đứng trên cột {remembered_column!r}, "
            "nhưng nguồn không còn coi cột đó là cursor dùng được (kiểu đã đổi, "
            "hoặc cột đã bị xoá) — sửa ở nguồn, hoặc nạp lại bằng mode 'full'"
        )
    return remembered


def _max_cursor(batch: pa.RecordBatch, cursor_column: str, cursor_type: str) -> str:
    """Mốc cao nhất của lô, ĐÃ tuần tự hoá bằng chính hàm mà server dùng để đọc.

    KHÔNG phải `str(max(...))`: giá trị này đi qua JSON tới
    `loom_core.cursor.parse_cursor_value`, nên định dạng là một HỢP ĐỒNG
    liên-service. `str(int)` thì an toàn, nhưng `str(datetime)` cho một dấu CÁCH
    thay vì `T` và chỉ chạy được vì `fromisoformat` của Python 3.11+ tình cờ nhận
    dấu cách — may, không phải thiết kế. `format_cursor_value` là phép nghịch
    tường minh của `parse_cursor_value`, và test khứ hồi qua cả sáu kiểu
    (`packages/core/tests/test_cursor.py`) là thứ bắt được một bên đổi định dạng
    mà bên kia không biết.

    `max()` chứ không "dòng cuối": nó đúng bất kể connector có ORDER BY hay
    không, và một watermark lấy từ dòng cuối của một lô KHÔNG sắp xếp sẽ tiến qua
    những giá trị lớn hơn nằm ở giữa lô — mất dòng, im lặng.
    """
    return format_cursor_value(cursor_type, max(batch.column(cursor_column).to_pylist()))


def run_incremental(
    connector: Connector,
    sink: Sink,
    client: IngestClientLike,
    stream: str,
    cursor: CursorCandidate,
    crash_after_batch: int | None = None,
) -> int:
    """GHI TRƯỚC, BÁO SAU. Đảo lại là mất dòng — xem test cùng tên.

    Nếu watermark được báo TRƯỚC khi lô được commit, một pod chết giữa hai bước
    làm watermark tiến qua dữ liệu chưa hề được ghi, và lần nạp sau bỏ qua đúng
    khoảng đó. Không có gì phát hiện được điều đó về sau — bảng chỉ đơn giản là
    thiếu dòng, không lỗi, không dấu vết. Thứ tự này cho TRÙNG chứ không cho
    MẤT, khớp hợp đồng at-least-once của spec mục 4.

    `crash_after_batch` chỉ để test nạp lại (`main.py` không truyền nó): nó ném
    `Boom` SAU khi lô thứ N đã ghi và đã báo, mô phỏng đúng chỗ đứt mà một
    OOMKill tạo ra.
    """
    total = 0
    for index, batch in enumerate(connector.read(stream, client.current_state())):
        enriched = add_bronze_columns(batch, client.source_id, uuid.uuid4())
        # 1. ghi VÀ commit lô này
        sink.append(enriched)
        # 2. chỉ khi (1) đã xong mới báo watermark
        client.report_progress(
            cursor_column=cursor.name,
            cursor_type=cursor.cursor_type,
            cursor_value=_max_cursor(batch, cursor.name, cursor.cursor_type),
            rows=batch.num_rows,
        )
        total += batch.num_rows
        if crash_after_batch is not None and index + 1 >= crash_after_batch:
            raise Boom("crash có chủ đích để kiểm nạp lại")
    return total
