"""Hợp đồng đứt-giữa-chừng của `incremental`: commit theo lô, nạp lại đi tiếp.

`FakeConnector` + hai sink ghi vào list. Không Iceberg, không mạng — cả hai được
kiểm ở integration. Ở đây chỉ kiểm THỨ TỰ và khả năng nạp lại, và đó là chỗ lỗi
mất-dòng sinh ra.

`batch_size=100` với 300 dòng là CÓ CHỦ ĐÍCH: ba lô, nên chuỗi sự kiện mong đợi
(`["write", "commit", "progress"] * 3` ở K = 1) phân biệt được "ghi rồi báo" với
"báo rồi ghi" ở NHIỀU hơn một lô. Với đúng một lô, hai chuỗi chỉ khác nhau ở hai
phần tử và một bản cài đặt gom hết rồi báo một lần cũng qua được.

**BA lô cũng là số nhỏ nhất làm nhóm K = 2 có một nhóm ĐỦ và một nhóm DỞ** — tức
là chỗ duy nhất bắt được lỗi "quên commit nhóm cuối", lỗi dễ nhất của Giai đoạn
3d và là lỗi mất dòng im lặng.
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


@pytest.mark.parametrize("commit_every", [1, 2, 3, 5])
def test_every_group_is_committed_before_its_watermark_is_reported(commit_every: int) -> None:
    """Đảo thứ tự = mất dòng, im lặng. Đúng với MỌI cỡ nhóm.

    Nếu báo watermark TRƯỚC khi commit, một pod chết giữa hai bước làm watermark
    tiến qua dữ liệu chưa hề bền, và lần nạp sau bỏ qua đúng khoảng đó. Không có
    gì phát hiện được điều đó về sau — bảng chỉ đơn giản là thiếu, không lỗi,
    không dấu vết.

    Canh bằng VỊ TRÍ TƯƠNG ĐỐI chứ không bằng một chuỗi viết cứng, vì một chuỗi
    viết cứng cho mỗi K là bốn chuỗi phải giữ khớp bằng tay: mỗi `progress` phải
    có một `commit` đứng NGAY TRƯỚC nó, và không `write` nào được nằm giữa hai
    cái đó. Bốn cỡ nhóm ở đây phủ cả nhóm chia hết (1, 3), nhóm có phần dư (2),
    và nhóm lớn hơn cả lần chạy (5).
    """
    events: list[tuple[str, int]] = []
    run_incremental(
        connector=_source(),
        sink=RecordingSink(events),
        client=RecordingClient(events),
        stream=_STREAM,
        cursor=_cursor(),
        commit_every_batches=commit_every,
    )
    kinds = [kind for kind, _ in events]

    assert kinds.count("progress") >= 1, kinds
    assert kinds.count("commit") == kinds.count("progress"), kinds
    for position, kind in enumerate(kinds):
        if kind == "progress":
            assert kinds[position - 1] == "commit", (
                f"lời báo ở vị trí {position} không có commit đứng ngay trước: {kinds}"
            )
    # Và mọi dòng đã đọc đều nằm trong một nhóm ĐÃ commit: lời gọi cuối cùng là
    # một `progress`, tức là không có lô nào ghi rồi bị bỏ lại sau nhóm cuối.
    assert kinds[-1] == "progress", kinds


def test_k_equals_one_is_exactly_the_per_batch_commit_of_phase_3a() -> None:
    """K = 1 phải TRÙNG hành vi 3a: một commit và một lời báo cho MỖI lô.

    Bài này là phép canh rằng việc nhóm lô là một phép TỔNG QUÁT HOÁ chứ không
    một đường đi thứ hai. Nếu K = 1 lệch khỏi hành vi cũ — gộp hai lô, bỏ một lời
    báo, đổi mốc báo về — thì mọi lập luận "hạ K về 1 là đường lùi an toàn" (xem
    `config.WriteTuning`) không còn đúng, và bài này là chỗ điều đó lộ ra.
    """
    events: list[tuple[str, int]] = []
    client = RecordingClient(events)
    run_incremental(_source(), RecordingSink(events), client, _STREAM, _cursor(), 1)

    assert [kind for kind, _ in events] == ["write", "commit", "progress"] * 3
    assert client.progress_calls == [
        ("id", "bigint", "99", 100),
        ("id", "bigint", "199", 100),
        ("id", "bigint", "299", 100),
    ]


def test_a_group_reports_the_last_batchs_watermark_and_the_whole_groups_rows() -> None:
    """MỘT lời báo cho cả nhóm: mốc của lô CUỐI, nhưng số dòng của CẢ NHÓM.

    Hai nửa, hai lý do khác nhau:

    * Mốc của lô cuối, vì sau commit thì cả nhóm đã bền — báo mốc của lô đầu
      nhóm là bỏ đi phần tiến độ vừa mua được và đọc lại nó ở lần chạy sau.
    * Số dòng của cả nhóm, vì `ingest_run.rows_written` CỘNG DỒN qua `/progress`.
      Báo `batch.num_rows` cho một nhóm K lô làm cột đó thiếu đúng K lần, và
      không có gì trong hệ thống mâu thuẫn với con số sai đó — giao diện 3c chỉ
      đơn giản hiển thị một phần ba số dòng đã nạp.
    """
    client = RecordingClient([])
    run_incremental(_source(), RecordingSink([]), client, _STREAM, _cursor(), 3)

    assert client.progress_calls == [("id", "bigint", "299", 300)]


def test_the_last_partial_group_is_committed_too() -> None:
    """Nhóm CUỐI dở dang phải được commit — nếu không thì mất dòng, mỗi lần chạy.

    Ba lô với K = 2: nhóm đầu đủ hai lô, còn lô thứ ba là cả một nhóm dở. Bỏ nó
    đi là bỏ 100 dòng mà KHÔNG có lỗi nào báo, và ở cỡ thật (50 lô, K = 20) là bỏ
    10 lô. Watermark cũng không tiến qua chúng, nên lần nạp sau đọc lại được —
    nhưng "được" chỉ đúng nếu có lần nạp sau, và một lần nạp `incremental` cuối
    cùng của một stream đã tắt thì không có.

    Khẳng định CẢ HAI mặt, qua hai sink khác nhau trên cùng một cấu hình: các
    `id` đã COMMIT (xem `CollectingSink` — nó cố ý không đếm lô mới ghi mà chưa
    commit), và chuỗi sự kiện đúng hình dạng "2 lô -> commit -> báo -> 1 lô ->
    commit -> báo". Chỉ số dòng thì một bản cài đặt commit nhóm cuối mà quên BÁO
    vẫn qua được; chỉ chuỗi sự kiện thì một bản `commit()` rỗng cũng qua được.
    """
    collecting = CollectingSink()
    run_incremental(_source(), collecting, RecordingClient([]), _STREAM, _cursor(), 2)
    assert sorted(collecting.ids) == list(range(_ROWS)), "nhóm cuối chưa được commit"

    events: list[tuple[str, int]] = []
    sink = RecordingSink(events)
    run_incremental(_source(), sink, RecordingClient(events), _STREAM, _cursor(), 2)
    assert [kind for kind, _ in events] == [
        "write",
        "write",
        "commit",
        "progress",
        "write",
        "commit",
        "progress",
    ]


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


@pytest.mark.parametrize("commit_every", [1, 2, 3, 5])
def test_a_crash_mid_run_loses_no_rows(commit_every: int) -> None:
    """Chép đúng cách `--crash-after-batch` đã kiểm ở 2c: chạy, giết, chạy lại,
    rồi khẳng định TẬP HỢP dòng đầy đủ. At-least-once cho phép trùng, không cho
    phép thiếu.

    CÙNG một `client` cho cả hai lần chạy, vì watermark sống ở control plane chứ
    không ở pod — một client mới cho lần hai sẽ là mô phỏng của một thế giới nơi
    watermark biến mất cùng pod, và bài test khi đó chỉ chứng minh rằng đọc lại
    từ đầu thì không thiếu gì.

    **Chạy qua BỐN cỡ nhóm vì cú đứt phải rơi vào cả hai loại chỗ.** Với K = 1 và
    K = 2, lô thứ 2 đóng một nhóm nên pod chết SAU một commit và SAU một lời báo
    watermark — lần chạy sau tiếp từ mốc đó. Với K = 3 và K = 5, nó chết GIỮA
    nhóm: hai lô đã ghi ra file Parquet nhưng chưa nhóm nào được commit, nên
    watermark chưa hề tiến và lần chạy sau đọc lại từ đầu. Chỉ vế thứ hai bắt
    được lỗi "báo watermark trước khi commit"; chỉ vế thứ nhất bắt được lỗi "báo
    mốc của lô đầu nhóm".

    `sink.ids` chỉ chứa dòng đã COMMIT (xem `CollectingSink`), nên nếu một nhóm
    được báo mà không được commit thì bài này ĐỎ với những `id` thiếu — đúng chỗ
    lỗi mất dòng nằm.
    """
    sink = CollectingSink()
    client = RecordingClient([])

    with pytest.raises(Boom):
        run_incremental(
            _source(), sink, client, _STREAM, _cursor(), commit_every, crash_after_batch=2
        )
    run_incremental(_source(), sink, client, _STREAM, _cursor(), commit_every)

    assert set(sink.ids) == set(range(_ROWS))


def test_at_least_once_means_a_committed_group_comes_back_after_a_crash() -> None:
    """Trùng lặp là CHUYỆN ĐÃ ĐƯỢC CHẤP NHẬN, không phải một lỗi bị bỏ qua.

    Lọc cursor là `>=` (xem bộ hợp đồng của connector), nên dòng mang đúng giá
    trị watermark xuất hiện lại ở lần chạy sau. Khẳng định nó ra để không ai
    "sửa" thành `>` — đó là đường dẫn tới mất dòng.

    K = 1 có chủ đích: đây là cấu hình duy nhất mà cú đứt ở lô 2 chắc chắn nằm
    SAU một commit, nên nó là cấu hình duy nhất mà dòng trùng chắc chắn xuất
    hiện. Ở K = 3 thì không nhóm nào kịp commit và tập dòng không có trùng nào —
    một hành vi khác, và nó được kiểm ở bài trên chứ không ở đây.
    """
    sink = CollectingSink()
    client = RecordingClient([])

    with pytest.raises(Boom):
        run_incremental(_source(), sink, client, _STREAM, _cursor(), 1, crash_after_batch=2)
    run_incremental(_source(), sink, client, _STREAM, _cursor(), 1)

    assert len(sink.ids) > _ROWS
    assert sink.ids.count(199) == 2


def test_a_death_at_commit_time_never_advances_the_watermark() -> None:
    """Pod chết ĐÚNG LÚC commit: watermark KHÔNG được nhích một chút nào.

    Đây là bài duy nhất trong bộ này bắt lỗi "báo watermark trước khi commit" bằng
    DỮ LIỆU chứ bằng thứ tự sự kiện. `crash_after_batch` không tới được chỗ đó —
    nó ném sau khi cả nhóm đã xong, nên hai thứ tự cho cùng một kết quả và cả hai
    đều "không mất dòng".

    Dựng lại đúng chỗ đứt: nhóm đầu (hai lô) vừa ghi xong, commit ném. Với thứ tự
    ĐÚNG, chưa lời báo nào được gửi nên watermark còn `None` và lần chạy sau đọc
    lại từ đầu — không mất dòng. Với thứ tự ĐẢO, watermark đã nhảy lên `"199"`
    trong khi 200 dòng đó chưa bao giờ vào bảng, nên lần chạy sau bắt đầu từ 199
    và các `id` 0..198 KHÔNG BAO GIỜ tới — bài này đỏ với đúng những `id` thiếu đó.

    CÙNG một sink cho cả hai lần chạy vì bảng bronze là một, và cùng một client vì
    watermark sống ở control plane (xem `test_a_crash_mid_run_loses_no_rows`).
    """
    sink = CollectingSink(die_at_commit=1)
    client = RecordingClient([])

    with pytest.raises(Boom):
        run_incremental(_source(), sink, client, _STREAM, _cursor(), 2)
    assert sink.ids == [], "không nhóm nào commit được thì không dòng nào hạ cánh"
    assert client.initial_cursor is None, "watermark tiến qua dữ liệu chưa commit"

    sink.die_at_commit = None
    run_incremental(_source(), sink, client, _STREAM, _cursor(), 2)

    assert set(sink.ids) == set(range(_ROWS)), "mất dòng"


def test_a_commit_group_smaller_than_one_batch_is_refused() -> None:
    """K = 0 nghĩa là "commit sau mỗi 0 lô" — không có nghĩa nào cả.

    Từ chối ồn ào chứ không âm thầm coi như 1: với `>=` trong điều kiện đóng nhóm,
    K = 0 sẽ commit sau MỖI lô (vì `1 >= 0`), tức là một cấu hình vô nghĩa lặng lẽ
    chạy như một cấu hình khác. Một người vận hành đặt `LOOM_TASK_COMMIT_EVERY_
    BATCHES=0` để "tắt việc nhóm lô" phải được nói rằng con số đó không tồn tại.
    (`WriteTuning` cũng chặn ở tầng cấu hình với `gt=0`; đây là lớp cho những
    người gọi không đi qua nó.)
    """
    with pytest.raises(ValueError, match="commit_every_batches"):
        run_incremental(_source(), CollectingSink(), RecordingClient([]), _STREAM, _cursor(), 0)


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


def test_a_hyphen_in_the_connection_name_becomes_an_underscore() -> None:
    """Tên connection có gạch NGANG là khuôn bình thường (`ItemCreate.name` canh
    `^[a-z0-9][a-z0-9-]*$`, và fixture của repo đặt tên kiểu `can-sua`), nhưng một
    gạch ngang trong tên bảng làm bảng đó không truy vấn được nếu không trích dẫn.

    Dựng lại thật trên DuckDB — engine mà `loom-query` chạy:
    `SELECT * FROM bronze.pos-aiven__public_orders` trả `ParserException: syntax
    error at or near "-"`; cùng câu đó với tên trong ngoặc kép thì chạy. Ví dụ của
    spec mục 5 cũng là `pos_aiven`, gạch DƯỚI.
    """
    assert bronze_table_name("pos-aiven", "public.orders") == "bronze.pos_aiven__public_orders"


def test_two_connection_names_never_encode_to_the_same_table() -> None:
    """Phép đổi `-` -> `_` là ĐƠN ÁNH trên bộ ký tự mà `item.name` cho phép.

    `^[a-z0-9][a-z0-9-]*$` không cho `_`, nên không có tên nào "đã sẵn" mang gạch
    dưới để trùng với một tên khác sau phép đổi. Nếu ràng buộc đó ở
    `ItemCreate.name` một ngày nào đó nới ra cho `_`, bài này đỏ — và nó PHẢI đỏ,
    vì lúc đó `pos-aiven` và `pos_aiven` là hai connection ghi vào MỘT bảng
    bronze, tức là hai nguồn trộn vào nhau trong im lặng.
    """
    assert bronze_table_name("pos-aiven", "s.t") != bronze_table_name("pos-aiveo", "s.t")
    assert bronze_table_name("a-b", "s.t") != bronze_table_name("a-b-c", "s.t")


def test_a_connection_name_with_two_hyphens_in_a_row_is_refused() -> None:
    """`pos--aiven` hợp lệ theo pattern nhưng thành `pos__aiven` — một dấu ngăn
    `__` THỨ HAI, và tên bảng không còn đọc ngược ra được nguồn.

    Từ chối ồn ào chứ không thu gọn `--` về một `_`: thu gọn thì `pos-aiven` và
    `pos--aiven` cho ra CÙNG một bảng bronze, và hai nguồn ghi đè lẫn nhau mà
    không có lỗi nào báo.
    """
    with pytest.raises(ValueError, match="pos--aiven"):
        bronze_table_name("pos--aiven", "public.orders")


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
