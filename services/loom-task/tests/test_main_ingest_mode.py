"""`main.ingest` chọn mode — bốn quyết định mà KHÔNG bài test nào khác chạm tới.

Bài này ra đời từ một lời nói dối trong repo: docstring của
`tests/integration/test_full_ingest.py` khẳng định "phần chọn mode của
`main.ingest` đã có test riêng" từ Task 12, và không có. `test_main_outcome.py`
kiểm `run_reporting_the_outcome`/`_source_dsn`/`target_table`;
`test_runner_full.py` và `test_runner_incremental.py` gọi thẳng `run_full`/
`run_incremental`. Cái ở GIỮA — nhánh `if spec.mode == "full"` cùng ba lời gọi
mà nó CỐ Ý bỏ qua — chưa từng chạy trong một bài test nào.

Và đó không phải một nhánh tầm thường. Docstring của `ingest` dành hai đoạn để
bảo vệ hai điều KHÔNG xảy ra ở đường `full` (`resolve_cursor` và `check_schema`),
với lập luận rằng làm ngược lại sẽ TỪ CHỐI đúng những bảng mà `full` tồn tại để
phục vụ. Một lập luận dài như thế mà không có phép canh nào là một lập luận sẽ
bị "dọn dẹp" bởi người đọc tiếp theo, người thấy hai đường xử lý lệch nhau và
làm cho chúng đều.

**Vì sao phải monkeypatch `_build_connector`/`_build_sink`.** Hai hàm đó mở kết
nối THẬT: `_build_connector` dựng `PostgresConnector` rồi gọi `check()`, còn
`_build_sink` gọi `build_catalog`, mà `RestCatalog.__init__` của PyIceberg bắn
`GET /v1/config` ngay lúc dựng. Không thay chúng thì bài này cần một Postgres
cộng một Lakekeeper để kiểm một nhánh `if` — đúng lý do `target_table` đã được
tách ra khỏi `_build_sink` (xem docstring của nó). Thay bằng double TỰ VIẾT chứ
không `unittest.mock`, cùng lý do đã ghi ở `doubles.py`: một `MagicMock` nhận
mọi lời gọi nên nó không giữ được hợp đồng nào.

Mọi thứ KHÁC trong đường đi đều là mã thật — `resolve_cursor`, `check_schema`,
`source_columns`, `run_full`, `run_incremental` đều là bản gốc, không double.
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
from loom_task.runner import CursorNotAvailable, SchemaDrift

_STREAM = "public.no_cursor"


class _Source:
    """Một nguồn hai cột `text` — tức KHÔNG có ứng viên cursor nào.

    `text` bị loại khỏi `CURSOR_TYPE_ALLOWLIST` có chủ đích (xem
    `loom_core.cursor`), nên `candidate_cursors` rỗng ở đây là hình dạng THẬT của
    một bảng chỉ có cột chuỗi — không phải một fake dựng cho tiện. Đó chính là
    lớp bảng mà `full` phải nạp được còn `incremental` thì không.

    `extra_column` để bài drift thêm một cột ở NGUỒN mà bảng bronze chưa có.
    """

    def __init__(self, *, rows: int = 3, extra_column: bool = False) -> None:
        self.discover_calls = 0
        self.read_calls: list[StreamState] = []
        self._names = ["id", "label"] + (["extra"] if extra_column else [])
        self._rows = rows

    def check(self) -> CheckResult:
        return CheckResult(ok=True, message="fake")

    def discover(self) -> list[StreamSchema]:
        self.discover_calls += 1
        return [
            StreamSchema(
                name=_STREAM,
                columns=tuple(
                    ColumnSchema(name=n, arrow_type=pa.string(), nullable=False)
                    for n in self._names
                ),
                candidate_cursors=(),
            )
        ]

    def read(self, stream: str, state: StreamState) -> Iterator[pa.RecordBatch]:
        assert stream == _STREAM
        self.read_calls.append(state)
        return iter(
            [
                pa.RecordBatch.from_pylist(
                    [{n: f"{n}-{i}" for n in self._names} for i in range(self._rows)]
                )
            ]
        )


class _SourceWithCursor(_Source):
    """Cùng nguồn, nhưng `id` là `bigint` — một ứng viên cursor dùng được."""

    def discover(self) -> list[StreamSchema]:
        self.discover_calls += 1
        return [
            StreamSchema(
                name=_STREAM,
                columns=tuple(
                    ColumnSchema(name=n, arrow_type=pa.string(), nullable=False)
                    for n in self._names
                ),
                candidate_cursors=(CursorCandidate(name="id", cursor_type="bigint"),),
            )
        ]


def _spec(mode: str) -> IngestSpec:
    return IngestSpec(
        run_id=uuid.uuid4(),
        lakehouse_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        connection_id=uuid.UUID("11111111-2222-3333-4444-555555555555"),
        connection_slug="pos-aiven",
        stream=_STREAM,
        mode=mode,  # type: ignore[arg-type]
        source=IngestSourceSpec(kind="postgres", host="db", port=5432, database="shop"),
    )


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """Trả về một hàm `wire(connector, sink)` gắn hai double vào `main`."""

    def wire(connector: object, sink: object) -> None:
        monkeypatch.setattr(main, "_build_connector", lambda spec: connector)
        monkeypatch.setattr(main, "_build_sink", lambda spec: sink)

    return wire


def test_full_ingests_a_table_that_has_no_usable_cursor(wired) -> None:  # type: ignore[no-untyped-def]
    """Một bảng không có cột nào dùng được làm watermark vẫn phải nạp `full` được.

    Đây là vế mà docstring `ingest` gọi là "một tính chất chứ không một tối ưu":
    hỏi cursor ở đường `full` sẽ từ chối đúng những bảng mà `full` tồn tại để
    phục vụ. Nguồn ở đây chỉ có cột `text`, tức `candidate_cursors` rỗng —
    `resolve_cursor` sẽ ném `CursorNotAvailable` nếu ai đó gọi nó.

    Khẳng định cả chuỗi sự kiện, không chỉ số dòng: `stage` (không phải `write`)
    chứng minh dữ liệu đi vào bảng STAGING, và ba bước tráo tên chứng minh nhánh
    `full` thật sự chạy chứ không phải một `run_incremental` tình cờ trả cùng số.
    """
    events: list[tuple[str, int]] = []
    source = _Source(rows=3)
    wired(source, RecordingSink(events))

    assert main.ingest(RecordingClient(events), _spec("full")) == 3

    kinds = [name for name, _ in events]
    assert kinds == [
        "stage",
        "progress",
        "staging_done",
        "rename_target_away",
        "promote_staging",
        "drop_old_target",
    ]


def test_full_never_asks_the_source_what_streams_it_has(wired) -> None:  # type: ignore[no-untyped-def]
    """`discover()` là một lượt đọc `information_schema` mà đường `full` không
    cần: nó không chọn cursor và không đối chiếu schema. Gọi nó vẫn "chạy đúng",
    nên chỉ một phép đếm mới giữ được tính chất này — và nó là điều kiện để bài
    trên có nghĩa (một `discover()` ở `full` là bước đầu của việc bò về phía
    `resolve_cursor`).
    """
    source = _Source()
    wired(source, RecordingSink([]))

    main.ingest(RecordingClient([]), _spec("full"))

    assert source.discover_calls == 0


def test_incremental_refuses_the_same_table(wired) -> None:
    """Cùng bảng, cùng double, chỉ đổi `mode` — và câu trả lời phải NGƯỢC lại.

    Hai bài đứng cạnh nhau mới nói được điều cần nói: `full` chạy được KHÔNG
    phải vì `resolve_cursor` dễ tính, mà vì đường `full` không gọi nó. Nếu ai đó
    kéo `resolve_cursor` lên trước nhánh `if`, bài này vẫn xanh còn bài đầu tiên
    đỏ — đúng chỗ cần đỏ.
    """
    events: list[tuple[str, int]] = []
    wired(_Source(), RecordingSink(events))

    with pytest.raises(CursorNotAvailable):
        main.ingest(RecordingClient(events), _spec("incremental"))

    # Không một lô nào được ghi: phép từ chối xảy ra TRƯỚC lô đầu tiên.
    assert events == []


def test_incremental_stops_on_schema_drift_before_writing_anything(wired) -> None:
    """Nguồn có thêm cột mà bảng bronze không có -> `SchemaDrift`, và KHÔNG một
    lô nào được ghi.

    Nguồn ở đây CÓ một cursor dùng được, có chủ đích: nó tách hai lý do từ chối
    ra khỏi nhau, nên khi bài này đỏ thì nó chỉ có thể vì `check_schema`, không
    bao giờ vì `resolve_cursor`.

    `events == []` là vế bắt lỗi. Ném `SchemaDrift` SAU khi vài lô đã hạ cánh
    cũng cho một run `failed` với đúng lý do đó, nhưng bảng bronze khi ấy đã có
    dữ liệu ghi dưới một schema mà chính ta vừa tuyên bố là không khớp.
    """
    events: list[tuple[str, int]] = []
    sink = RecordingSink(
        events,
        # Bảng bronze đã tồn tại với HAI cột nguồn + ba cột metadata. Ba cột
        # `_ingested_at`/`_source`/`_batch_id` phải có mặt, nếu không phép trừ
        # `BRONZE_COLUMNS` trong `check_schema` sẽ báo drift vì lý do khác hẳn
        # lý do bài này muốn dựng.
        columns=["id", "label", "_ingested_at", "_source", "_batch_id"],
    )
    wired(_SourceWithCursor(extra_column=True), sink)

    with pytest.raises(SchemaDrift, match="extra"):
        main.ingest(RecordingClient(events), _spec("incremental"))

    assert events == []


def test_full_is_the_way_out_of_schema_drift(wired) -> None:
    """CÙNG tình huống drift, mode `full` — và nó phải CHẠY.

    Đây là vế thứ hai của lý do `check_schema` không canh đường `full`: một lần
    nạp `full` chính là cách sửa tay mà thông báo của `SchemaDrift` chỉ tới. Canh
    cả `full` nghĩa là hệ thống từ chối luôn con đường thoát duy nhất nó có, và
    bài này là thứ sẽ đỏ vào ngày ai đó "làm cho hai đường đều nhau".
    """
    events: list[tuple[str, int]] = []
    sink = RecordingSink(events, columns=["id", "label", "_ingested_at", "_source", "_batch_id"])
    wired(_SourceWithCursor(extra_column=True, rows=2), sink)

    assert main.ingest(RecordingClient(events), _spec("full")) == 2


def test_full_reads_from_the_beginning_not_from_the_watermark(wired) -> None:
    """`full` đọc lại TỪ ĐẦU: `connector.read` phải nhận một `StreamState` RỖNG.

    Một watermark lọt vào đây biến lần nạp này thành `incremental` mang tên
    `full`, rồi cú tráo THAY cả bảng bằng đúng phần đuôi vừa đọc — mất dữ liệu,
    im lặng. `run_full` đã canh điều đó ở tầng của nó; ở đây phép canh đi qua
    `ingest`, tức là qua cả nhánh chọn mode, với một client CÓ watermark sẵn.
    """
    source = _Source()
    wired(source, RecordingSink([]))

    main.ingest(RecordingClient([], initial_cursor="900"), _spec("full"))

    assert source.read_calls == [StreamState()]
