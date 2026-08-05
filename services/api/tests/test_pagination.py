import uuid
from datetime import UTC, datetime

import pytest

from loom_api.pagination import CursorMismatch, Page, decode_cursor, encode_cursor


def test_round_trip():
    ts = datetime(2026, 8, 4, 12, 0, 0, 123456, tzinfo=UTC)
    key = uuid.uuid4()
    token = encode_cursor(ts, key, filters={"type": "pipeline"})
    got_ts, got_id = decode_cursor(token, filters={"type": "pipeline"})
    assert got_ts == ts
    assert got_id == key


def test_microseconds_survive():
    """Mất microsecond là mất tính duy nhất của khoá sắp xếp — đúng cái mà cursor
    dựa vào để không nhảy bản ghi."""
    ts = datetime(2026, 8, 4, 12, 0, 0, 999999, tzinfo=UTC)
    token = encode_cursor(ts, uuid.uuid4(), filters={})
    got_ts, _ = decode_cursor(token, filters={})
    assert got_ts.microsecond == 999999


def test_cursor_from_a_different_filter_is_rejected():
    """Không có kiểm này thì cursor của trang 1 với type=pipeline dùng cho truy
    vấn type=sql_script trả về rác, không có lỗi nào."""
    token = encode_cursor(datetime.now(UTC), uuid.uuid4(), filters={"type": "pipeline"})
    with pytest.raises(CursorMismatch):
        decode_cursor(token, filters={"type": "sql_script"})


def test_filter_order_does_not_matter():
    ts = datetime(2026, 8, 4, 12, 0, 0, 7, tzinfo=UTC)
    key = uuid.uuid4()
    token = encode_cursor(ts, key, filters={"x": "1", "y": "2"})
    # Khẳng định trên GIÁ TRỊ, không phải `key is not None`: decode_cursor luôn
    # trả về một UUID khi nó không ném, nên `is not None` đúng kể cả khi giá trị
    # sai — một câu khẳng định không nhìn thấy được thứ nó đặt tên.
    assert decode_cursor(token, filters={"y": "2", "x": "1"}) == (ts, key)


def test_garbage_cursor_is_rejected_not_crashed():
    for bad in ("", "khong-phai-base64", "!!!!", "YWJj"):
        with pytest.raises(CursorMismatch):
            decode_cursor(bad, filters={})


def test_page_reports_next_cursor_only_when_more_rows_exist():
    """`has_more` phải suy ra từ việc lấy limit+1 hàng, không phải từ COUNT(*).
    COUNT trên bảng đã lọc quyền là một truy vấn đắt thứ hai cho mỗi trang."""
    rows = [object() for _ in range(4)]
    page = Page.build(rows, limit=3, cursor_of=lambda _: "c")
    assert len(page.items) == 3
    assert page.next_cursor == "c"

    page2 = Page.build(rows[:2], limit=3, cursor_of=lambda _: "c")
    assert len(page2.items) == 2
    assert page2.next_cursor is None
