"""Hợp đồng đứt-giữa-chừng của `incremental`: commit theo lô, nạp lại đi tiếp.

`FakeConnector` + hai sink ghi vào list. Không Iceberg, không mạng — cả hai được
kiểm ở integration. Ở đây chỉ kiểm THỨ TỰ và khả năng nạp lại, và đó là chỗ lỗi
mất-dòng sinh ra.

`batch_size=100` với 300 dòng là CÓ CHỦ ĐÍCH: ba lô, nên chuỗi sự kiện mong đợi
(`["write", "progress"] * 3`) phân biệt được "ghi rồi báo" với "báo rồi ghi" ở
NHIỀU hơn một lô. Với đúng một lô, hai chuỗi chỉ khác nhau ở hai phần tử và một
bản cài đặt gom hết rồi báo một lần cũng qua được.
"""

from __future__ import annotations

import uuid

import pytest
from doubles import CollectingSink, RecordingClient, RecordingSink

from loom_connector import CursorCandidate, StreamSchema
from loom_connector.fake import FakeConnector
from loom_task.runner import (
    Boom,
    CursorNotAvailable,
    StreamNotDiscovered,
    add_bronze_columns,
    bronze_table_name,
    resolve_cursor,
    run_incremental,
)

_ROWS = 300
_BATCH = 100
# Stream duy nhất của `FakeConnector`, và `bigint` là kiểu nó khai cho `id` —
# lấy từ `discover()` trong `_cursor()` dưới đây chứ không viết cứng, để một lần
# đổi ở fake không để lại một hằng số nói dối ở đây.
_STREAM = "widgets"


def _source() -> FakeConnector:
    return FakeConnector(n_rows=_ROWS, batch_size=_BATCH)


def _cursor() -> CursorCandidate:
    return resolve_cursor(_source().discover(), _STREAM, None)


def test_every_batch_is_committed_before_its_watermark_is_reported() -> None:
    """Đảo thứ tự = mất dòng, im lặng.

    Nếu báo watermark TRƯỚC khi ghi, một pod chết giữa hai bước làm watermark
    tiến qua dữ liệu chưa hề được ghi, và lần nạp sau bỏ qua đúng khoảng đó.
    Không có gì phát hiện được điều đó về sau — bảng chỉ đơn giản là thiếu, không
    lỗi, không dấu vết.
    """
    events: list[tuple[str, int]] = []
    run_incremental(
        connector=_source(),
        sink=RecordingSink(events),
        client=RecordingClient(events),
        stream=_STREAM,
        cursor=_cursor(),
    )
    kinds = [kind for kind, _ in events]
    assert kinds == ["write", "progress"] * 3, kinds


def test_the_reported_watermark_is_the_highest_value_in_the_batch() -> None:
    """Mốc báo về là `max` của lô, tuần tự hoá bằng `format_cursor_value`.

    Ba giá trị `"99"/"199"/"299"` cũng là chỗ chứng minh phép so watermark KHÔNG
    phải so chuỗi: `"199" > "99"` là `False` theo từ điển, nên một `moves_forward`
    so chuỗi sẽ từ chối lô thứ hai và thứ ba — watermark kẹt ở `"99"` và mỗi lần
    nạp sau đọc lại gần như toàn bộ bảng.
    """
    client = RecordingClient([])
    run_incremental(_source(), RecordingSink([]), client, _STREAM, _cursor())
    assert client.progress_calls == [
        ("id", "bigint", "99", 100),
        ("id", "bigint", "199", 100),
        ("id", "bigint", "299", 100),
    ]
    assert client.initial_cursor == "299"


def test_resume_starts_from_the_reported_watermark() -> None:
    client = RecordingClient([], initial_cursor="100")
    sink = CollectingSink()
    run_incremental(_source(), sink, client, _STREAM, _cursor())
    assert min(sink.ids) == 100, "nạp lại phải bắt đầu từ watermark, không từ đầu"


def test_a_crash_mid_run_loses_no_rows() -> None:
    """Chép đúng cách `--crash-after-batch` đã kiểm ở 2c: chạy, giết, chạy lại,
    rồi khẳng định TẬP HỢP dòng đầy đủ. At-least-once cho phép trùng, không cho
    phép thiếu.

    CÙNG một `client` cho cả hai lần chạy, vì watermark sống ở control plane chứ
    không ở pod — một client mới cho lần hai sẽ là mô phỏng của một thế giới nơi
    watermark biến mất cùng pod, và bài test khi đó chỉ chứng minh rằng đọc lại
    từ đầu thì không thiếu gì.
    """
    sink = CollectingSink()
    client = RecordingClient([])

    with pytest.raises(Boom):
        run_incremental(_source(), sink, client, _STREAM, _cursor(), crash_after_batch=2)
    run_incremental(_source(), sink, client, _STREAM, _cursor())

    assert set(sink.ids) == set(range(_ROWS))
    # Và trùng lặp là CHUYỆN ĐÃ ĐƯỢC CHẤP NHẬN, không phải một lỗi bị bỏ qua: lọc
    # cursor là `>=` (xem bộ hợp đồng của connector), nên dòng mang đúng giá trị
    # watermark xuất hiện lại ở lần chạy sau. Khẳng định nó ra để không ai "sửa"
    # thành `>` — đó là đường dẫn tới mất dòng.
    assert len(sink.ids) > _ROWS
    assert sink.ids.count(199) == 2


def test_the_bronze_columns_carry_the_source_and_one_batch_id_per_batch() -> None:
    """Ba cột metadata (spec mục 5.5), và `_batch_id` là khoá khử trùng ở silver.

    Một `_batch_id` cho mỗi LÔ, không cho mỗi dòng: silver khử trùng bằng cách
    loại cả lô đã thấy, nên một id mỗi dòng làm cột đó vô dụng cho đúng việc nó
    tồn tại để làm.
    """
    batches = list(_source().read(_STREAM, RecordingClient([]).current_state()))
    first = add_bronze_columns(batches[0], "conn-1", uuid.UUID(int=1))

    assert first.schema.names[-3:] == ["_ingested_at", "_source", "_batch_id"]
    assert set(first.column("_source").to_pylist()) == {"conn-1"}
    assert len(set(first.column("_batch_id").to_pylist())) == 1
    assert first.num_rows == _BATCH


def test_bronze_table_name_separates_connection_from_table() -> None:
    """Một dấu gạch thôi thì `pos_public_orders` không phân biệt được connection
    `pos` đọc `public.orders` với connection `pos_public` đọc bảng `orders` —
    hai nguồn khác nhau ghi vào cùng một bảng, và không ai thấy cho tới khi số
    liệu sai."""
    assert bronze_table_name("pos", "public.orders") == "bronze.pos__public_orders"
    assert bronze_table_name("pos_public", "orders.x") == "bronze.pos_public__orders_x"
    assert bronze_table_name("a", "s.t") != bronze_table_name("a_s", "t.x")


def test_a_stream_without_a_schema_is_rejected() -> None:
    with pytest.raises(ValueError, match=r"schema\.table"):
        bronze_table_name("pos", "orders")


def test_the_cursor_type_comes_from_the_connector_not_from_a_guess() -> None:
    """Lần nạp ĐẦU TIÊN không có watermark, nên `IngestSpec.cursor_type` là
    `None` — mà `/progress` thì đòi nó ngay từ lô đầu. Chỗ duy nhất biết kiểu
    thật của cột ở nguồn là connector."""
    cursor = resolve_cursor(_source().discover(), _STREAM, None)
    assert (cursor.name, cursor.cursor_type) == ("id", "bigint")


def test_a_remembered_cursor_column_keeps_its_place() -> None:
    """Đổi cột giữa hai lần chạy làm `loom-api` ĐẶT LẠI watermark (hai thang đo
    khác nhau — xem `_advance_watermark`), tức là đọc lại từ đầu. Nên một khi đã
    chọn, `stream_state.cursor_column` là bên quyết định, không phải thứ tự của
    `candidate_cursors`."""
    cursor = resolve_cursor(_source().discover(), _STREAM, "updated_at")
    assert (cursor.name, cursor.cursor_type) == ("updated_at", "bigint")


def test_a_remembered_column_the_source_no_longer_offers_is_refused() -> None:
    """`ALTER TABLE ... TYPE text` ở nguồn làm cột đó rời `candidate_cursors`.
    Rơi về một cột khác sẽ lặng lẽ nạp lại toàn bộ bảng dưới cái tên
    "incremental"; ném thì có người đọc được lý do."""
    with pytest.raises(CursorNotAvailable, match="payload"):
        resolve_cursor(_source().discover(), _STREAM, "payload")


def test_a_stream_with_no_usable_cursor_column_is_refused() -> None:
    original = _source().discover()[0]
    without_cursors = [
        StreamSchema(name=original.name, columns=original.columns, candidate_cursors=())
    ]
    with pytest.raises(CursorNotAvailable, match="incremental"):
        resolve_cursor(without_cursors, _STREAM, None)


def test_an_unknown_stream_is_refused_before_any_read() -> None:
    with pytest.raises(StreamNotDiscovered, match="widgets"):
        resolve_cursor(_source().discover(), "public.orders", None)
