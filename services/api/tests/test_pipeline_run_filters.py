"""Chuẩn hoá tham số lọc của `GET /api/v1/pipeline-runs`, đo KHÔNG qua HTTP.

File này tồn tại vì một phép canh đi qua HTTP không đủ cho `?since=`. Lỗi mà
`_since_utc` chữa — một `datetime` naive bị asyncpg dịch theo giờ địa phương của
tiến trình API khi so với cột `timestamptz` — chỉ hiện ra trên máy có offset khác
0. Phép canh tích hợp tương ứng đỏ trên máy `+07` đã viết nó và XANH dưới
`TZ=UTC`, tức là mù đúng ở nơi CI chạy. Ở đây thì khẳng định là về INSTANT mà hàm
trả về, nên nó đỏ trên MỌI máy, không cần Docker và không cần đổi `TZ` của tiến
trình (một thủ thuật chạy được nhưng để lại trạng thái toàn cục trong một bộ test
chạy song song).

Cùng lý do `search_items_select` và `visible_pipeline_runs_select` là hàm có tên:
test phải gọi được đúng thứ chạy thật, chứ không dựng lại nó.
"""

from datetime import UTC, datetime, timedelta, timezone, tzinfo

from loom_api.routers.pipeline_runs import _since_utc


def test_a_mark_without_an_offset_is_read_as_utc() -> None:
    """Không offset nghĩa là UTC — cùng INSTANT, không dịch theo giờ máy.

    Đây là phép canh mà bản không chuẩn hoá làm đỏ ở MỌI múi giờ: nó so cả
    `utcoffset()` lẫn instant, nên một giá trị naive đi qua nguyên vẹn sẽ trượt ở
    dòng đầu tiên bất kể máy đang ở đâu.
    """
    naive = datetime(2026, 8, 18, 7, 30, 0)

    result = _since_utc(naive)

    assert result.utcoffset() == timedelta(0), "kết quả phải MANG offset, và offset đó là 0"
    assert result == datetime(2026, 8, 18, 7, 30, tzinfo=UTC)


def test_a_mark_with_an_offset_keeps_its_instant() -> None:
    """Mốc có offset được chuyển về UTC mà KHÔNG đổi thời điểm.

    `+07:00` là múi giờ của máy đã viết cả tính năng này, nên nó là trường hợp dễ
    lẫn nhất: một bản cài đặt `replace(tzinfo=UTC)` cho MỌI đầu vào (thay vì chỉ
    cho đầu vào naive) sẽ xanh ở phép canh trên và đỏ ở đây, lệch đúng 7 giờ.
    """
    aware = datetime(2026, 8, 18, 14, 30, 0, tzinfo=timezone(timedelta(hours=7)))

    result = _since_utc(aware)

    assert result == datetime(2026, 8, 18, 7, 30, tzinfo=UTC)
    assert result.utcoffset() == timedelta(0)


def test_a_mark_already_in_utc_comes_back_unchanged() -> None:
    """Trường hợp thường gặp nhất — giao diện gửi lại đúng mốc nó vừa nhận, kèm `Z`."""
    already = datetime(2026, 8, 18, 7, 30, tzinfo=UTC)

    assert _since_utc(already) == already


def test_the_two_ways_of_writing_one_instant_normalise_to_one_value() -> None:
    """`…T07:30:00` và `…T07:30:00+00:00` phải cho CÙNG một giá trị.

    Không phải chuyện thẩm mỹ: dấu vết cursor được tính TRÊN giá trị đã chuẩn hoá
    (xem `list_all_pipeline_runs`), nên nếu hai cách viết một mốc cho hai giá trị
    khác nhau thì cursor lấy ở dạng này bị dạng kia từ chối bằng 400, cho cùng một
    bộ lọc.
    """
    assert _since_utc(datetime(2026, 8, 18, 7, 30)) == _since_utc(
        datetime(2026, 8, 18, 7, 30, tzinfo=UTC)
    )


def test_an_offset_bearing_tzinfo_that_reports_no_offset_is_treated_as_naive() -> None:
    """`utcoffset()` chứ không `tzinfo is None` — lý do ở `loom_core/cursor.py`.

    `tzinfo` là một lớp trừu tượng, không phải một cờ: một thực thể hợp lệ được
    phép trả `None` cho offset, và khi đó Python vẫn xếp `datetime` là naive.
    `tzinfo is None` bỏ sót đúng trường hợp này, rồi `astimezone()` ném
    `ValueError` — tức một 500 thay vì một mốc UTC.

    Qua Pydantic thì đường HTTP hôm nay không dựng được một giá trị như vậy, nên
    đây là phép canh cho HÌNH DẠNG của phép kiểm, không cho một lỗi đang sống. Nó
    tồn tại để không ai "sửa" `utcoffset()` ngược về `tzinfo`.
    """

    class _NoOffset(tzinfo):
        def utcoffset(self, dt: datetime | None) -> timedelta | None:
            return None

        def dst(self, dt: datetime | None) -> timedelta | None:
            return None

        def tzname(self, dt: datetime | None) -> str | None:
            return None

    value = datetime(2026, 8, 18, 7, 30, tzinfo=_NoOffset())
    assert value.utcoffset() is None, "tiền đề: Python xếp giá trị này là naive"

    result = _since_utc(value)

    assert result == datetime(2026, 8, 18, 7, 30, tzinfo=UTC)
