"""Trần số dòng TRÙNG khi pod chết là một TÍCH, và phép canh phải khoá tích đó.

`batch_rows` và `commit_every_batches` nằm ở hai lớp cấu hình khác nhau, hỏng theo
hai kiểu khác nhau, và có hai docstring riêng — nhưng chúng NHÂN với nhau thành một
con số thứ ba mà không lớp nào trong hai lớp đó sở hữu:

    trần dòng trùng = batch_rows x commit_every_batches

Con số đó là một tính chất ĐỘ BỀN. Watermark chỉ tiến sau một commit thật
(`runner.run_incremental`), nên một pod chết giữa nhóm làm cả nhóm phải đọc lại, và
đúng ngần ấy dòng vào bảng bronze lần thứ hai. At-least-once cho phép trùng — nó
không cho phép trùng NHIỀU HƠN mức người ta nghĩ mình đã chọn.

**Vì sao một phép canh cho từng thừa số là KHÔNG ĐỦ, dù cả hai đã có.**
`test_read_tuning.py` khoá `batch_rows == 80.000` và `test_write_tuning.py` khoá
`commit_every_batches == 2`. Cả hai vẫn XANH khi có người nâng `batch_rows` lên
80.000 mà để K = 5 — vì mỗi bài chỉ thấy một nửa, và không bài nào biết con số của
bài kia. Đó chính là lối hồi quy mà bài này chặn: hai thay đổi mỗi cái hợp lý một
mình, cộng lại là nhân đôi số dòng trùng, và không có gì đỏ.

Đây KHÔNG phải một bài kiểm "hai hằng số bằng hai hằng số". Nó khẳng định ba điều
khác nhau, và mỗi điều bắt một loại thay đổi khác nhau:

  1. TÍCH đúng bằng con số đã chốt (bắt: đổi một thừa số, bất kể thừa số nào);
  2. tích KHÔNG tăng so với Giai đoạn 3a (bắt: một cặp mới được chọn "cho nhanh"
     mà làm hồi quy độ bền — hướng duy nhất không được đi);
  3. `run_incremental` THẬT SỰ đọc lại đúng ngần ấy dòng khi nó chết giữa nhóm
     (bắt: một thay đổi trong vòng lặp làm hai con số cấu hình mất nghĩa — hai
     hằng số khớp nhau không chứng minh được vòng lặp còn dùng chúng).
"""

from __future__ import annotations

from collections.abc import Iterator

import pyarrow as pa
import pytest
from doubles import RecordingClient, RecordingSink

from loom_connector import ColumnSchema, CursorCandidate, StreamSchema, StreamState
from loom_connector.protocol import CheckResult
from loom_task.config import ReadTuning, WriteTuning
from loom_task.runner import Boom, run_incremental

# Cặp đã chốt ở Giai đoạn 3d, và TÍCH của nó. Viết ra cả ba số chứ không tính lấy
# một: một khẳng định `a * b == a * b` luôn đúng và không canh được gì.
_BATCH_ROWS = 80_000
_COMMIT_EVERY = 2
_DUPLICATE_ROW_CEILING = 160_000

# TÍCH của Giai đoạn 3a (40.000 x 5). Có mặt để khẳng định thứ hai nói được HƯỚNG:
# trần được phép GIẢM, không được phép tăng.
_PHASE_3A_CEILING = 200_000


def test_the_duplicate_row_ceiling_is_the_product_of_both_knobs() -> None:
    """Hai mặc định phải nhân ra ĐÚNG trần đã chốt — đổi một cái là đỏ."""
    read = ReadTuning()
    write = WriteTuning()

    # TÍCH trước, từng thừa số sau — cố ý: khi ai đó đổi MỘT thừa số, câu đầu tiên
    # họ đọc phải là câu nói ra rằng trần độ bền vừa thay đổi, không phải một câu
    # `assert 5 == 2` về một con số lẻ.
    assert read.batch_rows * write.commit_every_batches == _DUPLICATE_ROW_CEILING, (
        f"trần dòng trùng = batch_rows x commit_every_batches = "
        f"{read.batch_rows:,} x {write.commit_every_batches} = "
        f"{read.batch_rows * write.commit_every_batches:,}, chốt là "
        f"{_DUPLICATE_ROW_CEILING:,}. Hai con số này KHÔNG độc lập — đổi một cái là "
        "đổi một tính chất ĐỘ BỀN (số dòng phải đọc lại khi pod chết giữa nhóm). "
        "Đổi cả hai CÙNG NHAU, rồi sửa con số ở đây và ở docstring `WriteTuning`."
    )
    assert read.batch_rows == _BATCH_ROWS
    assert write.commit_every_batches == _COMMIT_EVERY


def test_the_ceiling_never_rises_above_what_phase_3a_already_had() -> None:
    """Trần được phép GIẢM, không được phép TĂNG. Đây là vế nói ra hướng.

    Khẳng định thứ nhất bắt mọi thay đổi; khẳng định này nói vì sao một nửa số
    thay đổi đó là sai: 200.000 của 3a chưa từng được chứng minh là đúng, nhưng
    một cặp mới làm số dòng đọc-lại-sau-khi-chết TĂNG là một cuộc đổi mà Giai
    đoạn 3d không có quyền làm — 3d là hiệu năng, không phải độ bền.
    """
    assert _DUPLICATE_ROW_CEILING <= _PHASE_3A_CEILING, (
        f"trần mới {_DUPLICATE_ROW_CEILING:,} > trần 3a {_PHASE_3A_CEILING:,}: "
        "một thay đổi hiệu năng vừa làm hồi quy độ bền."
    )


class _ManyBatchSource:
    """Nguồn phát `batches` lô, mỗi lô `rows_per_batch` dòng, cursor là `id`.

    Con số ở đây là lô/dòng của một BÀI TEST, không phải `batch_rows` thật: bài
    này canh QUAN HỆ (mất tối đa một nhóm) chứ không canh 80.000 — dựng 80.000
    dòng Arrow trong một unit test là trả giá vài trăm MB RAM cho một tính chất
    không phụ thuộc vào cỡ.
    """

    def __init__(self, batches: int, rows_per_batch: int) -> None:
        self._batches = batches
        self._rows = rows_per_batch

    def check(self) -> CheckResult:
        return CheckResult(ok=True, message="double")

    def discover(self) -> list[StreamSchema]:
        return [
            StreamSchema(
                name="public.orders",
                columns=(ColumnSchema(name="id", arrow_type=pa.int64(), nullable=False),),
                candidate_cursors=(CursorCandidate(name="id", cursor_type="bigint"),),
            )
        ]

    def read(self, stream: str, state: StreamState) -> Iterator[pa.RecordBatch]:
        start = int(state.cursor_value) + 1 if state.cursor_value is not None else 0
        emitted = 0
        index = 0
        while emitted < self._batches * self._rows:
            base = start + index * self._rows
            yield pa.RecordBatch.from_pydict(
                {"id": pa.array([base + i for i in range(self._rows)], type=pa.int64())}
            )
            emitted += self._rows
            index += 1


def test_a_crash_mid_group_replays_at_most_one_whole_group() -> None:
    """Vòng lặp THẬT phải đọc lại tối đa `K x batch_rows` dòng, không hơn.

    Đây là vế bắt lỗi mà hai khẳng định hằng số ở trên KHÔNG bắt được: chúng so
    hai con số cấu hình với nhau, còn cái quyết định trần thật là thứ tự
    ghi/commit/báo trong `run_incremental`. Nếu một ngày nào đó vòng lặp báo
    watermark TRƯỚC commit thì hai hằng số vẫn khớp nhau hoàn hảo trong lúc trần
    thật thành 0 dòng trùng và một số dòng MẤT.

    Cách đo: cho nó chết ngay sau lô ĐẦU của một nhóm K = 2 (`crash_after_batch=1`),
    rồi chạy lại từ watermark mà client giữ được. Số dòng lô đầu đó phải được đọc
    LẠI — đúng một lô, tức đúng một nhóm dở dang, không phải cả lần chạy.
    """
    rows_per_batch = 4
    events: list[tuple[str, int]] = []
    client = RecordingClient(events)
    cursor = CursorCandidate(name="id", cursor_type="bigint")

    with pytest.raises(Boom):
        run_incremental(
            _ManyBatchSource(batches=4, rows_per_batch=rows_per_batch),
            RecordingSink(events),
            client,
            "public.orders",
            cursor,
            _COMMIT_EVERY,
            crash_after_batch=1,
        )

    # Lô đầu đã GHI nhưng nhóm chưa đủ K = 2 nên chưa commit, nên watermark không
    # được phép tiến — đó là cả hợp đồng ghi-trước-báo-sau.
    assert [kind for kind, _ in events] == ["write"], events
    assert client.current_state().cursor_value is None

    # Số dòng SẼ phải đọc lại = số dòng đã `write` mà chưa `commit` nào theo sau.
    # Đọc ra từ chuỗi sự kiện chứ không viết cứng: một vòng lặp commit sớm hơn/muộn
    # hơn sẽ đổi con số này, và đó chính là điều bài test muốn thấy.
    committed_upto = max((i for i, (kind, _) in enumerate(events) if kind == "commit"), default=-1)
    replayed_rows = sum(n for kind, n in events[committed_upto + 1 :] if kind == "write")
    assert replayed_rows == rows_per_batch
    assert replayed_rows <= _COMMIT_EVERY * rows_per_batch, (
        f"đọc lại {replayed_rows} dòng > trần một nhóm "
        f"({_COMMIT_EVERY} x {rows_per_batch}): vòng lặp không còn chặn mất-tiến-độ "
        "ở K lô, nên trần `batch_rows x K` không còn mô tả hệ thống."
    )

    events.clear()
    replayed = run_incremental(
        _ManyBatchSource(batches=4, rows_per_batch=rows_per_batch),
        RecordingSink(events),
        client,
        "public.orders",
        cursor,
        _COMMIT_EVERY,
    )

    # Lần chạy sau đọc lại từ ĐẦU (watermark chưa tiến) và đi tới hết, nên nó ghi
    # lại đúng cái lô đã ghi trước khi chết — trùng, không mất.
    assert replayed == 4 * rows_per_batch
    assert client.current_state().cursor_value == str(4 * rows_per_batch - 1)
