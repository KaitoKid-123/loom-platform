"""Mọi đường ra khỏi pod phải ĐÓNG run, và DSN nguồn phải chịu được mật khẩu thật.

Hai nhóm bài trong một file vì cả hai canh cùng một loại hỏng: một lỗi nói sai
chỗ. Một run kẹt ở `running` gửi người vận hành đi tìm một pod đã bị dọn; một DSN
bị cắt ở dấu `@` trong mật khẩu gửi họ đi tìm một lỗi DNS không tồn tại.
"""

from __future__ import annotations

import pytest
from doubles import RecordingClient

from loom_core.schemas import IngestSourceSpec
from loom_task.config import SourceCredentials
from loom_task.main import (
    SourceKindNotSupported,
    _describe,
    _source_dsn,
    run_reporting_the_outcome,
)


class _ClientThatCannotReport(RecordingClient):
    """`loom-api` không với tới được — lý do rất thường của một `complete` hỏng."""

    def complete(self, *, status: str, error: str | None = None) -> None:
        raise ConnectionError("loom-api không phản hồi")


def test_a_run_that_finishes_is_closed_without_an_error() -> None:
    """`succeeded` KÈM một dòng lỗi là một hàng tự mâu thuẫn, và
    `IngestCompletionReport` từ chối nó — `RecordingClient.complete` dựng chính
    model đó, nên bài này canh cả luật ở biên."""
    client = RecordingClient([])
    assert run_reporting_the_outcome(client, lambda: 42) == 42
    assert (client.status, client.error) == ("succeeded", None)


def test_an_unexpected_failure_closes_the_run_as_failed() -> None:
    """Một `KeyError` không ai lường cũng phải thành một run `failed` có lý do.

    Không có nhánh này, đúng những lỗi KHÔNG được dự đoán trước — loại duy nhất
    còn lại sau khi mọi lỗi dự đoán được đã có `except` riêng — là loại để run
    nằm lại ở `running` mãi.
    """
    client = RecordingClient([])
    with pytest.raises(KeyError):
        run_reporting_the_outcome(client, lambda: _raise(KeyError("cột lạ")))
    assert client.status == "failed"
    assert client.error is not None and "KeyError" in client.error


def test_the_failure_is_re_raised_so_kubernetes_sees_a_failed_job() -> None:
    """Nuốt ngoại lệ sau khi báo sẽ cho ra một Job "Complete" chứa một run
    `failed` — hai nguồn sự thật nói ngược nhau, và cái Kubernetes tin là cái
    sai."""
    client = RecordingClient([])
    with pytest.raises(ValueError, match="nguồn chết"):
        run_reporting_the_outcome(client, lambda: _raise(ValueError("nguồn chết")))


def test_an_exception_without_a_message_still_carries_a_reason() -> None:
    """`str(RuntimeError())` là chuỗi RỖNG, và `IngestCompletionReport` từ chối
    một run `failed` không có lý do. Ghép thẳng `str(exc)` nghĩa là chính lời báo
    lỗi bị từ chối, và run kẹt ở `running` VÌ đã hỏng."""
    client = RecordingClient([])
    with pytest.raises(RuntimeError):
        run_reporting_the_outcome(client, lambda: _raise(RuntimeError()))
    assert client.error == "RuntimeError"


def test_a_reason_longer_than_the_column_is_cut_not_refused() -> None:
    """`error` giới hạn 2000 ký tự ở schema. Một traceback dài phải bị CẮT, không
    được làm request `complete` thành 422 — lúc đó lỗi thật cũng mất luôn."""
    assert len(_describe(RuntimeError("x" * 5000))) == 2000


def test_a_failure_to_report_does_not_hide_the_original_error() -> None:
    """Lỗi cần đọc là lỗi ĐẦU TIÊN. Nếu `complete` hỏng và ngoại lệ của nó thay
    thế ngoại lệ gốc, người đọc log thấy "loom-api không phản hồi" và không bao
    giờ biết vì sao lần nạp hỏng."""
    client = _ClientThatCannotReport([])
    with pytest.raises(ValueError, match="schema nguồn đổi"):
        run_reporting_the_outcome(client, lambda: _raise(ValueError("schema nguồn đổi")))


def test_stopping_the_process_is_not_reported_as_a_failed_run() -> None:
    """`KeyboardInterrupt` không phải một lần nạp hỏng — nó là ai đó đang dừng
    tiến trình. Bắt `BaseException` ở đây sẽ ghi một lý do nói dối vào
    `ingest_run.error`."""
    client = RecordingClient([])
    with pytest.raises(KeyboardInterrupt):
        run_reporting_the_outcome(client, lambda: _raise(KeyboardInterrupt()))
    assert client.status is None


def test_a_password_with_uri_punctuation_does_not_split_the_dsn() -> None:
    """Mật khẩu chứa `@`/`/`/`:` đi thẳng vào URI sẽ CẮT chuỗi ở đúng ký tự đó,
    và libpq đọc ra một host khác. Triệu chứng ("could not translate host name")
    không nhắc gì tới mật khẩu, nên lỗi này tốn rất nhiều thời gian khi nó xảy
    ra."""
    dsn = _source_dsn(
        IngestSourceSpec(kind="postgres", host="db.internal", port=5432, database="shop"),
        SourceCredentials(source_user="loom@shop", source_password="p@ss/w:rd?x"),
    )
    assert dsn == "postgresql://loom%40shop:p%40ss%2Fw%3Ard%3Fx@db.internal:5432/shop"
    # Đúng MỘT `@` phân tách credential với host — điều kiện để libpq đọc đúng
    # host, và là thứ phép mã hoá ở trên tồn tại để bảo đảm.
    assert dsn.count("@") == 1


def test_a_source_without_a_database_is_refused() -> None:
    """Thiếu `database`, libpq nối vào một database MANG TÊN người dùng — một lần
    nạp từ sai database mà mọi thứ vẫn "chạy"."""
    with pytest.raises(SourceKindNotSupported, match="database"):
        _source_dsn(
            IngestSourceSpec(kind="postgres", host="db", port=5432, database=None),
            SourceCredentials(source_user="loom", source_password="x"),
        )


def test_a_source_kind_without_a_connector_is_refused() -> None:
    """`ConnectionDefinition.kind` cho phép `mysql`/`sqlserver`/`rest` từ Giai
    đoạn 1, nhưng 3a chỉ có `PostgresConnector`. Thử một DSN Postgres tới một
    cổng MySQL cho ra một lỗi giao thức không ai đọc được."""
    with pytest.raises(SourceKindNotSupported, match="mysql"):
        _source_dsn(
            IngestSourceSpec(kind="mysql", host="db", port=3306, database="shop"),
            SourceCredentials(source_user="loom", source_password="x"),
        )


def _raise(exc: BaseException) -> int:
    """`lambda` không chứa được `raise` — nó là một câu lệnh, không phải biểu
    thức. Hàm này là cách gọn nhất để một `Callable[[], int]` hỏng theo đúng
    kiểu ngoại lệ mà bài test muốn."""
    raise exc
