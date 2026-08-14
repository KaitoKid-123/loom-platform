"""Hợp đồng của `full`: bảng đích không bị chạm tới cho tới khi staging ghi xong.

Đây là tính chất THAY CHO "một commit ở cuối" mà ĐO 2 đã bác bỏ bằng số. Nó phải
được canh bằng THỨ TỰ SỰ KIỆN, vì một bản cài đặt ghi thẳng vào bảng đích rồi dọn
sau vẫn cho ra đúng số dòng, đúng nội dung, và đúng mọi phép kiểm khác — cho tới
ngày một lần nạp hỏng giữa chừng và xoá mất bảng của một người.

**Bài `test_full_commits_exactly_once_no_matter_how_many_batches` từng CỐ Ý KHÔNG
tồn tại, và giờ nó tồn tại.** Kế hoạch Task 12 đòi nó; ĐO 2 bác bỏ đúng tính chất
đó cho API mà nó đo — hai `tx.append` trong một `table.transaction()` của PyIceberg
0.11.1 cho 2 snapshot, không phải 1 — nên viết bài đó ra lúc ấy là khoá một lời
hứa sai vào bộ test. Giai đoạn 3d đo một API KHÁC (`Table.add_files`,
`scripts/probe_iceberg_add_files.py`) và nó gộp thật: N file vào ĐÚNG 1 snapshot ở
N = 1, 5, 20. Nên lời hứa giờ đúng, và nó có phép canh.

Điều KHÔNG được đọc ngược: ĐO 2 vẫn đúng nguyên. Cái sai là câu tổng quát
"PyIceberg 0.11.1 không gộp được commit", nếu ai đó đã rút ra nó từ ĐO 2.

`_ROWS`/`_BATCH` cho BA lô có chủ đích, cùng lý do như `test_runner_incremental`:
với đúng một lô, "ghi hết vào staging rồi tráo" và "tráo rồi ghi" chỉ khác nhau ở
hai phần tử, và một bản cài đặt sai vẫn qua được.
"""

from __future__ import annotations

import uuid

import pytest
from doubles import RecordingClient, RecordingSink

from loom_connector.fake import FakeConnector
from loom_task.runner import Boom, run_full
from loom_task.sink import old_target_name, staging_table_name

_ROWS = 300
_BATCH = 100
_STREAM = "widgets"  # stream duy nhất của `FakeConnector`
_TARGET = "bronze.pos__public_orders"


def _source() -> FakeConnector:
    return FakeConnector(n_rows=_ROWS, batch_size=_BATCH)


def test_full_writes_everything_to_staging_before_touching_the_target() -> None:
    """Bảng đích KHÔNG được chạm tới cho tới khi staging đã ghi xong.

    Đây là tính chất thay cho "một commit" mà ĐO 2 đã bác bỏ. Một bản cài ghi
    thẳng vào đích rồi mới dọn sẽ làm mọi phép kiểm khác vẫn xanh, cho tới ngày
    một lần nạp hỏng giữa chừng và xoá mất bảng của ai đó.
    """
    sink = RecordingSink([])
    run_full(_source(), sink, RecordingClient([]), _STREAM)
    kinds = [kind for kind, _ in sink.events]

    # Khẳng định SỰ CÓ MẶT trước rồi mới cắt: `kinds.index("staging_done")` trên
    # một danh sách không có nó ném `ValueError` — một bài test "hỏng" thay vì
    # một bài test ĐỎ nói được điều gì, và đúng đột biến quan trọng nhất (bỏ
    # staging, ghi thẳng vào đích) là đột biến làm sự kiện đó biến mất.
    assert "staging_done" in kinds, f"không có bước chốt staging nào: {kinds}"
    before_swap = kinds[: kinds.index("staging_done")]
    assert set(before_swap) == {"stage"}, f"trước khi chốt staging chỉ được ghi staging: {kinds}"
    assert "rename_target_away" not in before_swap


def test_the_swap_renames_the_target_away_before_promoting_staging() -> None:
    """Thứ tự ba bước là vấn đề ĐÚNG-SAI, không phải sở thích.

    `drop(đích)` rồi `rename(staging -> đích)` để lại một cửa sổ mà nếu rename
    hỏng thì dữ liệu cũ đã mất và dữ liệu mới chưa vào. Ba bước giữ dữ liệu cũ
    sống dưới một tên khác tới bước cuối, nên hỏng ở giữa còn lùi lại được.
    """
    sink = RecordingSink([])
    run_full(_source(), sink, RecordingClient([]), _STREAM)
    kinds = [kind for kind, _ in sink.events]
    assert kinds[-3:] == ["rename_target_away", "promote_staging", "drop_old_target"], kinds


def test_the_very_first_full_load_has_no_target_to_rename_away() -> None:
    """Lần nạp `full` ĐẦU TIÊN của một lakehouse: chưa có bảng đích.

    Bước `rename(đích -> đích_cũ)` trên một bảng không tồn tại là
    `NoSuchTableError`. Đây là đường mà MỌI lakehouse mới đi qua đúng một lần,
    nên nếu nó hỏng thì tính năng hỏng ngay lần dùng đầu — và không có test nào
    khác đi qua nó, vì mọi bài còn lại giả định đã có bảng đích.
    """
    sink = RecordingSink([], has_target=False)
    run_full(_source(), sink, RecordingClient([]), _STREAM)
    kinds = [kind for kind, _ in sink.events]

    assert kinds[-2:] == ["staging_done", "promote_staging"], kinds
    assert "rename_target_away" not in kinds, "không có gì để đổi tên đi"
    assert "drop_old_target" not in kinds, "không có `đích_cũ` nào để xoá"


def test_full_reports_rows_but_never_a_cursor() -> None:
    """Số dòng thì báo; watermark thì KHÔNG — hai tính chất, một lời gọi.

    Báo dòng vì `ingest_run.rows_written` chỉ cộng dồn qua `/progress`
    (`/complete` không mang `rows`), nên một `run_full` im lặng để cột đó ở 0 và
    người dùng không có con số nào để xem trong lúc nạp.

    KHÔNG báo cursor vì `full` đọc lại từ đầu (spec mục 5): không có mốc nào để
    tiến, và một watermark đẩy lên ở đây làm lần `incremental` KẾ TIẾP bỏ qua đúng
    khoảng dữ liệu vừa đọc — mất dòng, im lặng, và ở một lần chạy khác nên không
    ai nối được hậu quả với nguyên nhân.

    So CẢ BỐN phần tử của mỗi lời gọi, không chỉ đếm số lời gọi: chính ba `None`
    kia là tính chất. Một bản cài đặt chép lời gọi `report_progress` từ
    `run_incremental` sang vẫn báo đúng số dòng, và chỉ ba `None` này bắt được nó.
    """
    client = RecordingClient([], initial_cursor="500")
    run_full(_source(), RecordingSink([]), client, _STREAM)

    assert client.progress_calls == [(None, None, None, _BATCH)] * 3
    assert client.initial_cursor == "500", "watermark cũ phải nguyên vẹn, không bị đặt lại"
    assert client.cursor_column == "id", "cột watermark cũ cũng không được đổi"


def test_full_reads_the_whole_table_even_when_a_watermark_already_exists() -> None:
    """`full` đọc lại TỪ ĐẦU, kể cả khi stream này đã có watermark.

    `loom-api` đã không gửi watermark cho mode này (xem `ingest_spec`), nên bài
    này canh lớp thứ hai: `run_full` truyền `StreamState()` RỖNG cho connector
    thay vì `client.current_state()`. Đọc watermark ở đây biến `full` thành một
    lần `incremental` mang tên `full` — rồi cú tráo THAY cả bảng bằng đúng phần
    đuôi vừa đọc. Đó là mất dữ liệu, và nó không để lại lỗi nào.
    """
    client = RecordingClient([], initial_cursor="100")
    assert run_full(_source(), RecordingSink([]), client, _STREAM) == _ROWS


def test_a_crash_while_writing_staging_never_touches_the_target() -> None:
    """Đứt khi đang ghi staging: bảng đích phải NGUYÊN VẸN.

    Khẳng định mạnh hơn "không có rename nào": chuỗi sự kiện chỉ được chứa
    `stage`. Một bản cài đặt ghi thẳng vào bảng đích cũng không sinh ra rename
    nào — nó chỉ đơn giản là đã phá bảng đích rồi.
    """
    sink = RecordingSink([])
    with pytest.raises(Boom):
        run_full(_source(), sink, RecordingClient([]), _STREAM, crash_after_batch=2)
    kinds = [kind for kind, _ in sink.events]

    assert set(kinds) == {"stage"}, f"chỉ được chạm staging: {kinds}"
    assert "rename_target_away" not in kinds
    assert "promote_staging" not in kinds


def test_full_commits_exactly_once_no_matter_how_many_batches() -> None:
    """BA lô, MỘT commit — và cái commit đó là `staging_done`, không phải `stage`.

    Đây là tính chất mà Giai đoạn 3d mua được: ĐO 3 định giá commit catalog ở
    44,0% đồng hồ tường vì đường nạp commit mỗi lô, và `add_files` hạ N file vào
    một snapshot (đo thật: 50 file / 1 snapshot / 3,2 s so với 50 lần `append` /
    47,9 s).

    Đếm `stage` cũng cần thiết chứ không chỉ đếm commit: một bản cài đặt gom cả
    ba lô vào RAM rồi ghi một file duy nhất cũng cho "một commit", và nó là đúng
    cái đánh đổi RAM mà cả đường nạp này không được phép làm (trần pod 512Mi).
    """
    sink = RecordingSink([])
    run_full(_source(), sink, RecordingClient([]), _STREAM)
    kinds = [kind for kind, _ in sink.events]

    assert kinds.count("stage") == _ROWS // _BATCH == 3
    assert kinds.count("staging_done") == 1, kinds


def test_a_crash_while_writing_staging_commits_nothing() -> None:
    """Đứt khi đang ghi staging: hai lô đã GHI, và KHÔNG commit nào xảy ra.

    Từ Giai đoạn 3d, `stage` ghi một file Parquet mà chưa đăng ký nó vào bảng, nên
    những lô đã ghi trước cú đứt KHÔNG đọc được — chúng là object rác trên S3 dưới
    thư mục của một bảng staging mà không lần chạy nào sau đó nhìn tới. Đó là hạ
    cấp so với 3a (chỗ chúng thật sự nằm trong bảng staging) và nó KHÔNG mất gì:
    bảng staging của một lần chạy chết vốn đã bị bỏ hẳn — hậu tố `run_id` nghĩa là
    không lần chạy nào sau đó tìm lại nó (xem `staging_table_name`), nên "đã
    commit" chưa bao giờ mua được một lần nạp tiếp.

    Cái phải giữ nguyên là bảng ĐÍCH, và `test_a_crash_while_writing_staging_never_
    touches_the_target` canh nó. Bài này canh vế còn lại: không có `staging_done`
    nào, tức là cú tráo chưa bao giờ bắt đầu.
    """
    sink = RecordingSink([])
    with pytest.raises(Boom):
        run_full(_source(), sink, RecordingClient([]), _STREAM, crash_after_batch=2)
    assert sink.events == [("stage", _BATCH), ("stage", _BATCH)]


def test_two_runs_never_collide_on_the_old_target_name() -> None:
    """Tên `đích_cũ` mang hậu tố theo `run_id`, nên hai lần chạy không đụng nhau.

    ĐO 2 mục D4: `rename_table` TỪ CHỐI đè lên một tên đang tồn tại. Với một tên
    cố định, một lần chạy chết giữa bước 2 và bước 4 để lại `đích_cũ`, và lần
    chạy SAU hỏng ở bước 2 — rồi mọi lần sau nữa cũng vậy. Tính năng tự khoá
    chính nó, vĩnh viễn, tới khi có người vào xoá tay.
    """
    dead_run = old_target_name(_TARGET, uuid.uuid4())
    next_run = old_target_name(_TARGET, uuid.uuid4())
    assert dead_run != next_run


def test_two_runs_never_collide_on_the_staging_name() -> None:
    """Cùng lập luận cho bảng staging: `create_from` trên một tên đã tồn tại
    ném lỗi, nên hai run song song trên cùng một stream (chuyện XẢY RA ĐƯỢC —
    `start_ingest` chưa có cổng chống trùng) phải ghi vào hai bảng khác nhau."""
    assert staging_table_name(_TARGET, uuid.uuid4()) != staging_table_name(_TARGET, uuid.uuid4())


def test_the_names_are_deterministic_for_one_run() -> None:
    """Cùng `run_id` cho cùng một tên, mọi lúc. Một tên sinh ngẫu nhiên mỗi lần
    gọi làm `promote_staging` đi tìm một bảng mà `stage` không hề ghi vào."""
    run_id = uuid.uuid4()
    assert staging_table_name(_TARGET, run_id) == staging_table_name(_TARGET, run_id)
    assert old_target_name(_TARGET, run_id) == old_target_name(_TARGET, run_id)


def test_both_derived_names_stay_in_the_targets_namespace() -> None:
    """Cùng namespace với bảng đích, và đó là một điều kiện đúng-sai.

    ĐO 2 mục D chỉ đo `rename_table` TRONG một namespace; đổi tên qua namespace
    khác là hành vi CHƯA ĐO của Lakekeeper. Và `target_exists()` dựa vào việc
    namespace `bronze` đã tồn tại lúc nó được gọi — chính `stage()` vừa tạo nó.
    """
    run_id = uuid.uuid4()
    for name in (staging_table_name(_TARGET, run_id), old_target_name(_TARGET, run_id)):
        assert name.startswith("bronze.")
        assert name.count(".") == 1, f"{name} phải là 'bronze.<tên>', không thêm tầng namespace"


def test_the_derived_names_have_no_hyphens_to_quote() -> None:
    """`run_id.hex`, không `str(run_id)`. Dấu gạch ngang trong một UUID biến tên
    bảng thành một định danh phải trích dẫn trong mọi câu SQL chạm tới nó — kể cả
    câu SELECT tay của người đi dọn rác mà hai cái tên này để lại."""
    assert "-" not in staging_table_name(_TARGET, uuid.uuid4())
    assert "-" not in old_target_name(_TARGET, uuid.uuid4())
