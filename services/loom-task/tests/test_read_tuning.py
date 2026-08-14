"""Cỡ lô ĐỌC phải đi từ cấu hình TỚI connector — không dừng lại ở giữa.

Bài này ra đời từ một lỗi đã chạy thật: `_build_connector` dựng
`PostgresConnector(dsn=...)` mà KHÔNG truyền `batch_rows`, nên production im
lặng chạy ở mặc định 10 000 của connector suốt Giai đoạn 3a. Không có gì hỏng,
không có dòng log nào lạ — chỉ là ĐO 3 đo đường nạp ở 1,5 MB/s trong khi cùng
đường đó với 40 000 dòng/lô cho 3,6 MB/s.

**Vì sao phép canh phải đi QUA `_build_connector` chứ không chỉ đọc
`ReadTuning().batch_rows`.** Một bài kiểm mặc định của lớp cấu hình vẫn XANH
trong đúng cái lỗi trên: giá trị vẫn đúng 40 000, chỉ là không ai đọc nó. Thứ
duy nhất bắt được là một phép canh nhìn vào con số mà `PostgresConnector` THẬT
SỰ nhận — nên double dưới đây ghi lại `kwargs` rồi bài test khẳng định trên
chính chúng.

`PostgresConnector` được thay bằng double TỰ VIẾT (cùng lý do đã ghi ở
`doubles.py` và `test_main_ingest_mode.py`): bản thật mở kết nối ngay trong
`check()`, nên bài này sẽ cần một Postgres chỉ để đếm một tham số.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from loom_connector.protocol import CheckResult
from loom_core.schemas import IngestSourceSpec, IngestSpec
from loom_task import main

# Mặc định riêng của `PostgresConnector` (xem `loom_connector.postgres`). Viết ra
# ở đây để bài test nói được điều nó thật sự canh: KHÔNG phải "40 000 là một số
# đẹp", mà "giá trị tới nơi KHÁC với giá trị connector tự chọn khi không ai nói
# gì". Hai con số bằng nhau sẽ làm cả bài này vô nghĩa mà vẫn xanh.
_CONNECTOR_OWN_DEFAULT = 10_000


class _RecordingConnector:
    """Ghi lại `kwargs` mà `_build_connector` gọi tới, rồi vờ như nguồn sống."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs

    def check(self) -> CheckResult:
        # `_build_connector` gọi `check()` và ném `SourceUnreachable` nếu thất
        # bại — trả `ok=True` để bài test đi tới được câu lệnh `return`.
        return CheckResult(ok=True, message="double")


@pytest.fixture
def built(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """Trả về hàm `build()` chạy `_build_connector` với connector đã bị thay.

    Hai biến `LOOM_TASK_SOURCE_*` là bắt buộc: `SourceCredentials` từ chối một
    mật khẩu rỗng có chủ đích (xem `config.py`), nên thiếu chúng thì bài test
    hỏng vì một lý do chẳng liên quan gì tới cỡ lô.
    """
    monkeypatch.setenv("LOOM_TASK_SOURCE_USER", "loom")
    monkeypatch.setenv("LOOM_TASK_SOURCE_PASSWORD", "secret")

    recorded: list[_RecordingConnector] = []

    def _fake(**kwargs: Any) -> _RecordingConnector:
        connector = _RecordingConnector(**kwargs)
        recorded.append(connector)
        return connector

    monkeypatch.setattr(main, "PostgresConnector", _fake)

    def build() -> dict[str, Any]:
        main._build_connector(_spec())
        assert len(recorded) == 1
        return recorded[0].kwargs

    return build


def test_the_connector_is_built_with_the_configured_batch_size(  # type: ignore[no-untyped-def]
    built, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`LOOM_TASK_BATCH_ROWS` phải đi hết đường tới `PostgresConnector`.

    Một giá trị KHÁC hẳn hai mặc định (của `ReadTuning` lẫn của connector) để
    con số trong khẳng định không thể tới từ chỗ nào khác ngoài biến môi trường.
    """
    monkeypatch.setenv("LOOM_TASK_BATCH_ROWS", "25000")

    assert built()["batch_rows"] == 25_000


def test_the_batch_size_defaults_to_the_measured_value_not_the_connector_one(built) -> None:  # type: ignore[no-untyped-def]
    """KHÔNG đặt biến nào -> connector vẫn phải nhận 40 000, không phải 10 000.

    Đây là vế bắt đúng lỗi đã có: bỏ `batch_rows` khỏi lời gọi thì
    `PostgresConnector` tự lấy 10 000 của nó và mọi thứ vẫn "chạy". Khẳng định
    thứ hai (`!= _CONNECTOR_OWN_DEFAULT`) là thứ nói ra điều đó thành lời — nếu
    một ngày nào đó hai mặc định trùng nhau, bài này phải ĐỎ để người sửa biết
    rằng nó đã ngừng canh được gì.
    """
    kwargs = built()

    assert kwargs["batch_rows"] == 40_000
    assert kwargs["batch_rows"] != _CONNECTOR_OWN_DEFAULT


def _spec() -> IngestSpec:
    return IngestSpec(
        run_id=uuid.uuid4(),
        lakehouse_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        connection_id=uuid.UUID("11111111-2222-3333-4444-555555555555"),
        connection_slug="pos-aiven",
        stream="public.orders",
        mode="full",
        source=IngestSourceSpec(kind="postgres", host="db", port=5432, database="shop"),
    )
