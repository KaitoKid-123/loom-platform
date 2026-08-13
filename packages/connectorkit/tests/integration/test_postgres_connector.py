"""Test riêng của `PostgresConnector`.

Bộ hợp đồng dùng chung (`packages/connectorkit/tests/test_connector_contract.py`)
đã canh bảy hành vi mà MỌI `Connector` phải có, chạy giống hệt trên `fake` lẫn
`postgres`. Bộ này canh những gì CHỈ Postgres mới có: cursor phía server thật,
ép kiểu NUMERIC/timestamptz thật qua information_schema thật, `check()` trên
một host không nghe (không phải một fake luôn trả `ok=False` theo yêu cầu).
"""

from __future__ import annotations

import resource

import pyarrow as pa
import pytest

from loom_connector.postgres import PostgresConnector
from loom_connector.protocol import StreamState

pytestmark = pytest.mark.integration

# Đã đo bằng script rời (không giữ trong repo) trên CÙNG hình dạng dữ liệu mà
# `large_seeded_source` dựng (~50 MB payload, lấy đúng một lô 100 dòng):
# cursor có tên tăng RSS tiến trình +0.4 MiB, cursor thường (bỏ `name=`) tăng
# +98.8 MiB cho CÙNG một lô. Ngưỡng dưới đây nằm giữa hai con số đó với biên
# rộng cả hai phía (~50 lần trên mức "named cursor thật", ~5 lần dưới mức
# "cursor thường") để không nhạy với dao động RSS bình thường của tiến trình
# Python (import, GC, ...) nhưng vẫn bắt được đúng lớp lỗi cần bắt.
_MATERIALIZE_THRESHOLD_MIB = 20.0


def test_check_reports_ok_for_a_reachable_source(source_dsn: str) -> None:
    result = PostgresConnector(dsn=source_dsn).check()
    assert result.ok is True
    assert result.message != ""


def test_check_returns_failure_instead_of_raising_for_an_unreachable_host() -> None:
    """`check()` là điều UI gọi sau nút "Test connection" (spec Giai đoạn 3c)
    — một stack trace không phải một thông báo người vận hành đọc được.

    Cổng 1 trên loopback cần quyền root để bind nên chắc chắn không ai đang
    lắng nghe ở đó — kết nối bị HỆ ĐIỀU HÀNH từ chối ngay lập tức (ECONNREFUSED),
    không cần chờ timeout nào, nên bài này không tốn thời gian chờ.
    """
    connector = PostgresConnector(dsn="postgresql://nobody:nobody@127.0.0.1:1/nope")
    result = connector.check()
    assert result.ok is False
    assert result.message != ""


def test_discover_finds_the_table_columns_and_right_candidate_cursors(
    seeded_source: tuple[str, str, int, int],
) -> None:
    dsn, stream_name, _, _ = seeded_source
    connector = PostgresConnector(dsn=dsn)
    stream = next(s for s in connector.discover() if s.name == stream_name)
    assert {c.name for c in stream.columns} == {"id", "placed_at", "amount", "note"}
    # `amount` là NUMERIC — sắp xếp được nhưng không đảm bảo tăng dần theo
    # thời gian chèn. `note` là TEXT — sắp theo từ điển, không theo thời gian.
    # Chỉ còn "id" và "placed_at" là watermark AN TOÀN.
    #
    # Khẳng định cả KIỂU, không chỉ tên: chuỗi kiểu này đi nguyên văn tới
    # `/internal/ingest/{run_id}/progress` và bị `CURSOR_TYPE_ALLOWLIST` kiểm ở
    # đó (xem `CursorCandidate`), nên nó phải là tên của `information_schema`
    # (`timestamp with time zone`) chứ không phải tên `pg_catalog` (`timestamptz`
    # — đúng chữ mà `CREATE TABLE` trong fixture dùng).
    assert {(c.name, c.cursor_type) for c in stream.candidate_cursors} == {
        ("id", "integer"),
        ("placed_at", "timestamp with time zone"),
    }


def test_nullable_is_read_from_the_source_not_guessed(
    seeded_source: tuple[str, str, int, int],
) -> None:
    dsn, stream_name, _, _ = seeded_source
    connector = PostgresConnector(dsn=dsn)
    stream = next(s for s in connector.discover() if s.name == stream_name)
    by_name = {c.name: c for c in stream.columns}
    assert by_name["note"].nullable is True
    assert by_name["id"].nullable is False
    assert by_name["placed_at"].nullable is False
    assert by_name["amount"].nullable is False


def test_read_produces_the_expected_number_of_batches(
    seeded_source: tuple[str, str, int, int],
) -> None:
    dsn, stream_name, n_rows, batch_rows = seeded_source
    connector = PostgresConnector(dsn=dsn, batch_rows=batch_rows)
    batches = list(connector.read(stream_name, StreamState()))
    expected_batches = (n_rows + batch_rows - 1) // batch_rows  # chia lấy trần
    assert len(batches) == expected_batches
    assert sum(b.num_rows for b in batches) == n_rows


def test_read_does_not_materialize_before_the_first_batch(
    large_seeded_source: tuple[str, str, int],
) -> None:
    """Đây là bài quan trọng nhất của file này — nó canh đúng thứ cursor CÓ
    TÊN tồn tại để ngăn (xem docstring đầu `postgres.py`).

    Một bản cài gọi `fetchall()` (hay dùng cursor THƯỜNG rồi `fetchmany()`
    sau đó) vẫn cho ra ĐÚNG SỐ LƯỢNG batch và ĐÚNG SỐ DÒNG mỗi batch — đếm
    batch không phân biệt được hai cách cài đặt, vì cursor thường của psycopg
    đã kéo hết bảng về RAM CLIENT ngay trong `execute()`, trước khi
    `fetchmany()` đầu tiên chạy, và `fetchmany()` sau đó chỉ cắt từ bộ nhớ đã
    có sẵn — bên ngoài không thấy khác biệt gì qua giá trị trả về.

    Cách bắt: đo RSS tiến trình (`ru_maxrss` — mốc CAO NHẤT, không giảm khi
    RAM được giải phóng, cùng lý lẽ với `scripts/measure_ingest_pod.py`) ngay
    trước khi gọi `read()` và ngay sau khi lấy được đúng MỘT batch (không gọi
    `list()`). Với cursor có tên, chỉ 100 dòng đã về tới Python — chênh lệch
    nhỏ. Với cursor thường, cả ~50 MB payload đã về tới Python trước đó —
    chênh lệch lớn hơn ngưỡng `_MATERIALIZE_THRESHOLD_MIB` nhiều lần.
    """
    dsn, stream_name, batch_rows = large_seeded_source
    connector = PostgresConnector(dsn=dsn, batch_rows=batch_rows)

    before_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    result = connector.read(stream_name, StreamState())
    first_batch = next(result)
    after_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    assert first_batch.num_rows == batch_rows
    delta_mib = (after_kib - before_kib) / 1024
    assert delta_mib < _MATERIALIZE_THRESHOLD_MIB, (
        f"RSS tăng {delta_mib:.1f} MiB chỉ để lấy MỘT lô {batch_rows} dòng trên "
        "một bảng ~50 MB — nghi ngờ read() đã kéo hết bảng về client thay vì "
        "dùng cursor phía server"
    )


def test_incremental_read_includes_the_boundary_row(
    seeded_source: tuple[str, str, int, int],
) -> None:
    """Lọc `>=`, KHÔNG `>` — xem docstring `_read_rows` trong `postgres.py`.
    Dòng có `id` ĐÚNG BẰNG mốc watermark bắt buộc phải có mặt lại sau resume."""
    dsn, stream_name, n_rows, _ = seeded_source
    connector = PostgresConnector(dsn=dsn, batch_rows=n_rows)
    full = list(connector.read(stream_name, StreamState()))
    ids = sorted(v for b in full for v in b.column("id").to_pylist())
    boundary = ids[len(ids) // 2]

    resumed = list(
        connector.read(stream_name, StreamState(cursor_column="id", cursor_value=str(boundary)))
    )
    resumed_ids = [v for b in resumed for v in b.column("id").to_pylist()]
    assert resumed_ids, "lọc theo cursor không được trả rỗng khi còn dòng phía sau mốc"
    assert min(resumed_ids) == boundary
    assert boundary in resumed_ids, (
        f"dòng id={boundary} (mốc watermark) bị thiếu sau khi resume — lọc đang "
        "là '>' chứ không phải '>=', nghĩa là dữ liệu mất thật"
    )


def test_arrow_types_survive_the_round_trip(seeded_source: tuple[str, str, int, int]) -> None:
    dsn, stream_name, n_rows, _ = seeded_source
    connector = PostgresConnector(dsn=dsn, batch_rows=n_rows)
    batches = list(connector.read(stream_name, StreamState()))
    table = pa.Table.from_batches(batches)

    assert pa.types.is_integer(table.schema.field("id").type)
    assert pa.types.is_timestamp(table.schema.field("placed_at").type)
    # NUMERIC -> Arrow string, KHÔNG float64 — xem docstring `postgres.py`.
    assert pa.types.is_string(table.schema.field("amount").type)

    ids = table.column("id").to_pylist()
    amounts = table.column("amount").to_pylist()
    idx = ids.index(0)
    assert amounts[idx] == "19.99", (
        "NUMERIC phải giữ ĐÚNG chuỗi thập phân đã insert, không được ép qua "
        "float rồi định dạng lại (đó là đúng lớp lỗi mất chính xác đã bị cấm)"
    )
