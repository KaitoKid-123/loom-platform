"""Cỡ NHÓM COMMIT phải đi từ cấu hình TỚI vòng lặp nạp — không dừng lại ở giữa.

Bài này là bản song song của `test_read_tuning.py`, và nó tồn tại vì cùng một lỗi
đã chạy thật một lần: `_build_connector` dựng `PostgresConnector` mà quên truyền
`batch_rows`, nên production im lặng chạy ở mặc định của connector suốt Giai đoạn
3a — không có gì hỏng, không một dòng log nào lạ, chỉ là hơn một nửa thông lượng
biến mất. `run_incremental` có mặc định RIÊNG (K = 1, hành vi 3a) đúng cùng hình
dạng đó, nên nó có đúng cùng cái bẫy.

**Vì sao bài này ĐẾM SỰ KIỆN thay vì ghi lại tham số.** Một double ghi lại
`kwargs` của `run_incremental` chứng minh được rằng con số đã được TRUYỀN, nhưng
không rằng nó được DÙNG — và ở đây tồn tại một bản cài đặt sai đọc được con số
rồi bỏ qua nó. Đếm số lần `commit` trên chuỗi sự kiện của `RecordingSink` canh cả
hai đầu bằng một khẳng định: BA lô với K = 2 phải cho ĐÚNG hai commit (một nhóm
đủ, một nhóm dở), K = 1 cho ba, K = 3 cho một.

`_build_connector`/`_build_sink` bị thay vì cả hai mở kết nối thật — cùng lý do
đã ghi ở `test_main_ingest_mode.py`, và bằng double TỰ VIẾT chứ không
`unittest.mock`, cùng lý do đã ghi ở `doubles.py`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pyarrow as pa
import pytest
from doubles import RecordingClient, RecordingSink

from loom_connector import ColumnSchema, CursorCandidate, StreamSchema, StreamState
from loom_connector.protocol import CheckResult
from loom_core.schemas import IngestSourceSpec, IngestSpec
from loom_task import main
from loom_task.config import WriteTuning

_STREAM = "public.orders"
_BATCHES = 3
_BATCH_ROWS = 2


class _ThreeBatchSource:
    """Nguồn phát ĐÚNG ba lô, với một cột `id` dùng được làm cursor.

    Ba lô là số nhỏ nhất phân biệt được ba cỡ nhóm mà bài này kiểm: K = 1 (ba
    nhóm), K = 2 (một nhóm đủ + một nhóm dở), K = 3 (một nhóm). Với hai lô thì
    K = 2 và K = 3 cho cùng một câu trả lời và bài test mất một nửa sức.
    """

    def check(self) -> CheckResult:
        return CheckResult(ok=True, message="double")

    def discover(self) -> list[StreamSchema]:
        return [
            StreamSchema(
                name=_STREAM,
                columns=(ColumnSchema(name="id", arrow_type=pa.int64(), nullable=False),),
                candidate_cursors=(CursorCandidate(name="id", cursor_type="bigint"),),
            )
        ]

    def read(self, stream: str, state: StreamState) -> Iterator[pa.RecordBatch]:
        assert stream == _STREAM
        for index in range(_BATCHES):
            base = index * _BATCH_ROWS
            yield pa.RecordBatch.from_pydict(
                {"id": pa.array([base + i for i in range(_BATCH_ROWS)], type=pa.int64())}
            )


def _spec() -> IngestSpec:
    return IngestSpec(
        run_id=uuid.uuid4(),
        lakehouse_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        connection_id=uuid.UUID("11111111-2222-3333-4444-555555555555"),
        connection_slug="pos-aiven",
        stream=_STREAM,
        mode="incremental",
        source=IngestSourceSpec(kind="postgres", host="db", port=5432, database="shop"),
    )


def _commits_with(monkeypatch: pytest.MonkeyPatch, configured: str | None) -> list[str]:
    """Chạy `main.ingest` một lần và trả về chuỗi sự kiện của sink."""
    if configured is None:
        monkeypatch.delenv("LOOM_TASK_COMMIT_EVERY_BATCHES", raising=False)
    else:
        monkeypatch.setenv("LOOM_TASK_COMMIT_EVERY_BATCHES", configured)

    events: list[tuple[str, int]] = []
    monkeypatch.setattr(main, "_build_connector", lambda spec: _ThreeBatchSource())
    monkeypatch.setattr(main, "_build_sink", lambda spec: RecordingSink(events))

    assert main.ingest(RecordingClient(events), _spec()) == _BATCHES * _BATCH_ROWS
    return [kind for kind, _ in events]


@pytest.mark.parametrize(
    ("configured", "expected_commits"),
    [("1", 3), ("2", 2), ("3", 1), ("99", 1)],
)
def test_the_configured_commit_group_reaches_the_ingest_loop(
    monkeypatch: pytest.MonkeyPatch, configured: str, expected_commits: int
) -> None:
    """`LOOM_TASK_COMMIT_EVERY_BATCHES` phải đi hết đường tới `run_incremental`.

    `99` (lớn hơn cả số lô) có mặt vì nó là hình dạng của một lần nạp nhỏ chạy
    dưới một cấu hình đặt cho lần nạp lớn: một nhóm duy nhất, dở dang, và nó vẫn
    PHẢI được commit — chỗ đó là lỗi mất dòng dễ nhất của Giai đoạn 3d.
    """
    kinds = _commits_with(monkeypatch, configured)

    assert kinds.count("commit") == expected_commits, kinds
    assert kinds.count("write") == _BATCHES, kinds
    assert kinds[-1] == "progress", kinds


def test_the_default_group_is_the_measured_one_not_the_loop_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """KHÔNG đặt biến nào -> phải là mặc định của `WriteTuning`, không của vòng lặp.

    Vế thứ hai là vế bắt lỗi: `run_incremental` mặc định K = 1 để một người gọi
    quên tham số rơi vào hành vi 3a đã kiểm, nên nếu `main.ingest` cũng quên
    truyền thì mọi thứ vẫn "chạy" — chỉ là commit vẫn từng lô và cả Giai đoạn 3d
    không có hiệu lực nào trong production. Với ba lô, K = 5 cho MỘT commit còn
    K = 1 cho ba, nên hai bản cài đặt đó không cho cùng một con số.
    """
    assert WriteTuning().commit_every_batches == 5
    assert _commits_with(monkeypatch, None).count("commit") == 1


def test_a_group_of_zero_batches_is_refused_at_the_config_layer() -> None:
    """`gt=0`: một cấu hình vô nghĩa phải chết ở chỗ nó được ĐỌC.

    `run_incremental` cũng từ chối K < 1, nhưng nó từ chối sau khi connector đã
    mở kết nối tới nguồn — muộn hơn, và với một traceback đi qua hai lớp.
    """
    with pytest.raises(ValueError, match="commit_every_batches"):
        WriteTuning(commit_every_batches=0)
