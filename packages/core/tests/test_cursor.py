"""So sánh watermark theo KIỂU — trái tim của đường báo tiến độ, kiểm không DB.

Nằm ở đây chứ không ở `services/api/tests/`: mã bị kiểm sống ở
`loom_core.cursor` (xem docstring module đó cho lý do nó không ở trong
`loom-api` lẫn trong `loom-connector`), và quy ước của repo là test nằm cạnh
package của nó. Cả hai chỗ đều chạy trong `make test` không cần Docker, nên
điều kiện thật sự quan trọng — phép so sánh này kiểm được mà không cần
Postgres — được giữ nguyên.
"""

from __future__ import annotations

import datetime as dt

import pytest

from loom_core.cursor import (
    CURSOR_TYPE_ALLOWLIST,
    CursorTypeNotAllowed,
    CursorValueUnusable,
    format_cursor_value,
    moves_forward,
    parse_cursor_value,
)

# Một giá trị Python cho MỖI kiểu trong allowlist. Bảng này bị khẳng định là
# ĐẦY ĐỦ ngay dưới đây, nên thêm một kiểu vào allowlist mà quên nó là một test
# đỏ, không phải một kiểu chưa ai kiểm phép tuần tự hoá.
_SAMPLE_BY_TYPE: dict[str, object] = {
    "smallint": 32_767,
    "integer": 2_147_483_647,
    # Chữ số nhiều hơn `integer` có chủ đích: nếu một bản cài đặt nào đó ép về
    # int32 thì nó tràn ở đây chứ không im lặng.
    "bigint": 9_223_372_036_854_775_807,
    "date": dt.date(2026, 8, 13),
    "timestamp without time zone": dt.datetime(2026, 8, 13, 0, 0, 1, 123_456),
    "timestamp with time zone": dt.datetime(2026, 8, 13, 0, 0, 1, 123_456, tzinfo=dt.UTC),
}


def test_the_round_trip_table_covers_every_allowed_type() -> None:
    """Bảng mẫu ở trên phải phủ ĐÚNG allowlist — không thiếu, không thừa.

    Không có dòng này, thêm một kiểu vào `CURSOR_TYPE_ALLOWLIST` mà quên thêm
    mẫu chỉ làm bài khứ hồi chạy ít hơn một trường hợp, và nó vẫn xanh. Kiểu
    mới đó là kiểu DUY NHẤT chưa ai kiểm hợp đồng chuỗi cho nó.
    """
    assert set(_SAMPLE_BY_TYPE) == CURSOR_TYPE_ALLOWLIST


def test_the_allowlist_is_exactly_the_six_types_the_connector_offers() -> None:
    """Chốt chống-trôi giữa hai bên dùng chung hằng số này.

    `loom_connector.postgres.discover()` lọc `candidate_cursors` bằng CHÍNH
    frozenset này, còn `loom-api` từ chối `cursor_type` ngoài nó. Viết thẳng sáu
    chuỗi ở đây (không dẫn lại từ mã) là cách duy nhất để một lần thêm/bớt kiểu
    trở thành một dòng nhìn thấy được trong diff, thay vì một thay đổi hợp đồng
    lặng lẽ giữa hai service.
    """
    assert {
        "smallint",
        "integer",
        "bigint",
        "date",
        "timestamp without time zone",
        "timestamp with time zone",
    } == CURSOR_TYPE_ALLOWLIST


@pytest.mark.parametrize("cursor_type", sorted(CURSOR_TYPE_ALLOWLIST))
def test_every_allowed_type_can_be_parsed(cursor_type: str) -> None:
    """Không kiểu nào lọt vào allowlist mà bộ phân tích không biết đọc.

    Đó đúng là dạng hỏng mà một allowlist viết tay mời gọi: thêm `"numeric"`
    vào frozenset là một dòng, và nó qua được cổng 422 ở biên rồi nổ thành 500
    ở `parse_cursor_value`. `CURSOR_TYPE_ALLOWLIST` được DỰNG từ chính những
    hằng số mà hàm đó phân nhánh trên (xem `loom_core.cursor`), và test này là
    thứ khẳng định điều đó vẫn đúng.
    """
    sample = {
        "smallint": "1",
        "integer": "1",
        "bigint": "1",
        "date": "2026-08-13",
        "timestamp without time zone": "2026-08-13T00:00:01",
        "timestamp with time zone": "2026-08-13T00:00:01+00:00",
    }[cursor_type]
    assert parse_cursor_value(cursor_type, sample) is not None


@pytest.mark.parametrize("cursor_type", sorted(CURSOR_TYPE_ALLOWLIST))
def test_a_cursor_value_survives_the_round_trip_for_every_allowed_type(cursor_type: str) -> None:
    """`parse(format(x)) == x` cho CẢ SÁU kiểu — hợp đồng chuỗi giữa hai service.

    Giá trị này đi từ `loom-task` (`_max_cursor` trong `loom_task.runner`) qua
    JSON tới `loom-api`, nơi `parse_cursor_value` đọc lại nó. Không ai canh thì
    một bên đổi định dạng và bên kia không biết.

    **Phép khứ hồi MỘT MÌNH không đủ, và đây là chỗ dễ tưởng là đủ nhất.** Nếu
    nhánh timestamp đổi từ `isoformat()` sang `str()` — tức từ `'…T00:00:01'`
    sang `'… 00:00:01'`, dấu CÁCH thay cho `T` — thì `parse(format(x)) == x`
    VẪN XANH, vì `datetime.fromisoformat` của CPython 3.11+ nhận cả dấu cách.
    Đã kiểm thật trên 3.12:

        dt.datetime.fromisoformat('2026-08-13 00:00:01+00:00')  -> đọc được

    Nên bài này khẳng định THÊM một tính chất của chính chuỗi trên dây: không
    kiểu nào trong sáu kiểu có dạng chuẩn chứa dấu cách. Đó không phải chép lại
    bản cài đặt — nó là điều kiện để chuỗi này còn đọc được bởi một bên phân
    tích KHÔNG phải `fromisoformat` của CPython (Postgres, một parser JS, một
    service viết bằng ngôn ngữ khác), và nó là thứ duy nhất bắt được đúng đột
    biến trên.
    """
    value = _SAMPLE_BY_TYPE[cursor_type]
    rendered = format_cursor_value(cursor_type, value)

    assert parse_cursor_value(cursor_type, rendered) == value
    assert " " not in rendered, (
        f"{cursor_type}: {rendered!r} mang dấu cách — dạng chuẩn của cả sáu kiểu "
        "không có dấu cách nào; xem docstring bài này"
    )


def test_formatting_refuses_a_value_that_is_not_of_the_type_it_claims() -> None:
    """Ném ở BÊN GỬI, không đẩy một chuỗi sai kiểu sang cho biên bên nhận.

    Cả bốn cặp dưới đây đều là lỗi chọn nhầm cột làm cursor, và cả bốn đều sẽ
    thành 422 ở `IngestProgressReport` nếu lọt qua đây — một 422 mô tả CHUỖI
    thay vì mô tả GIÁ TRỊ, cách chỗ gây ra nó một chặng mạng.
    """
    with pytest.raises(CursorValueUnusable):
        format_cursor_value("bigint", dt.datetime(2026, 8, 13, tzinfo=dt.UTC))
    with pytest.raises(CursorValueUnusable):
        format_cursor_value("bigint", "1000")
    # `datetime` LÀ lớp con của `date`: không loại nó ra thì một cột timestamp
    # khai là `date` cho ra `'2026-08-13T00:00:01'`, và `date.fromisoformat` ném.
    with pytest.raises(CursorValueUnusable):
        format_cursor_value("date", dt.datetime(2026, 8, 13, 0, 0, 1))
    # `bool` LÀ lớp con của `int` — `True` mà thành `'True'` thì đầu kia từ chối.
    with pytest.raises(CursorValueUnusable):
        format_cursor_value("bigint", True)


def test_formatting_applies_the_same_offset_rule_as_parsing() -> None:
    """Hai đầu của cùng một luật, để chúng không thể trôi khỏi nhau.

    `parse_cursor_value` từ chối một `timestamptz` không có offset (và ngược
    lại) vì Python không so được naive với aware. Nếu bên GỬI không có cùng
    phép kiểm thì lỗi đó vẫn xảy ra, chỉ là ở xa hơn một chặng mạng.
    """
    with pytest.raises(CursorValueUnusable, match="offset"):
        format_cursor_value("timestamp with time zone", dt.datetime(2026, 8, 13, 0, 0, 1))
    with pytest.raises(CursorValueUnusable, match="offset"):
        format_cursor_value(
            "timestamp without time zone", dt.datetime(2026, 8, 13, 0, 0, 1, tzinfo=dt.UTC)
        )


def test_formatting_refuses_a_type_outside_the_allowlist() -> None:
    with pytest.raises(CursorTypeNotAllowed):
        format_cursor_value("numeric", 1)


def test_an_integer_cursor_compares_numerically_not_lexicographically() -> None:
    """CẶP GIÁ TRỊ LÀ CỐ Ý và nó là toàn bộ lý do test này tồn tại.

    `("400", "200")` — cặp mà một bản kế hoạch trước dùng — XANH y nguyên với
    một bản cài đặt so sánh CHUỖI, tức là chứng nhận đúng con bug cần chặn.
    `"1000"` so với `"400"` phân biệt được hai bản cài đặt: so sánh SỐ cho
    1000 > 400 (tiến), so sánh CHUỖI cho "1000" < "400" (lùi).
    """
    assert moves_forward("bigint", "400", "1000") is True
    # Vế nghịch, trên CÙNG cặp giá trị: nếu ai đó "sửa" phép so thành chuỗi thì
    # dòng trên đỏ, còn dòng này xanh — hai dòng cạnh nhau nói rõ hướng nào là
    # tiến, thay vì để người đọc phải suy.
    assert moves_forward("bigint", "1000", "400") is False
    # Bằng nhau KHÔNG phải tiến — `<`, không `<=`.
    assert moves_forward("bigint", "400", "400") is False


def test_the_pair_that_string_comparison_gets_right_is_not_enough() -> None:
    """Ghi lại lý do cặp `("400","200")` bị loại làm phép kiểm CHÍNH.

    Nó vẫn đúng về mặt hành vi, nên nó ở lại — nhưng ở đây, dưới một cái tên nói
    rõ rằng nó KHÔNG phân biệt được so-sánh-số với so-sánh-chuỗi. Xoá nó đi thì
    người sau lại thêm nó vào và tưởng mình đã canh xong.
    """
    assert moves_forward("bigint", "400", "200") is False
    assert "400" > "200"  # so sánh chuỗi TÌNH CỜ đúng ở đúng cặp này


def test_a_smallint_and_an_integer_cursor_follow_the_same_rule() -> None:
    """Ba kiểu nguyên đi CÙNG một nhánh — nói ra để không ai canh mỗi `bigint`
    rồi tưởng cả nhóm đã được kiểm."""
    assert moves_forward("smallint", "9", "10") is True
    assert moves_forward("integer", "9", "10") is True


def test_a_timestamp_cursor_moves_forward_too() -> None:
    """Cùng luật, kiểu khác. Timestamp ISO-8601 sắp xếp CHUỖI lại ĐÚNG, nên nó
    là nửa số kiểu mà con bug so-sánh-chuỗi KHÔNG lộ ra — một bộ test chỉ có
    timestamp sẽ xanh trên đúng bản cài đặt hỏng."""
    older, newer = "2026-08-12T23:59:59", "2026-08-13T00:00:01"
    assert moves_forward("timestamp without time zone", older, newer) is True
    assert moves_forward("timestamp without time zone", newer, older) is False


def test_a_date_cursor_moves_forward_too() -> None:
    assert moves_forward("date", "2026-08-12", "2026-08-13") is True
    assert moves_forward("date", "2026-08-13", "2026-08-12") is False


def test_an_offset_bearing_timestamp_compares_across_offsets() -> None:
    """Hai giá trị `timestamp with time zone` so theo THỜI ĐIỂM, không theo chữ.

    `2026-08-13T06:00:00+07:00` là 23:00 UTC ngày 12 — SỚM HƠN
    `2026-08-13T00:00:01+00:00`, dù chuỗi của nó lớn hơn theo từ điển. Đây là
    trường hợp mà cả so-sánh-chuỗi LẪN một phép so bỏ qua offset đều sai.
    """
    earlier_instant = "2026-08-13T06:00:00+07:00"
    later_instant = "2026-08-13T00:00:01+00:00"
    assert earlier_instant > later_instant  # từ điển nói ngược
    assert moves_forward("timestamp with time zone", earlier_instant, later_instant) is True
    assert moves_forward("timestamp with time zone", later_instant, earlier_instant) is False


def test_parsing_returns_the_python_type_the_name_promises() -> None:
    assert parse_cursor_value("bigint", "1000") == 1000
    assert parse_cursor_value("date", "2026-08-13") == dt.date(2026, 8, 13)
    assert parse_cursor_value("timestamp without time zone", "2026-08-13T00:00:01") == dt.datetime(
        2026, 8, 13, 0, 0, 1
    )
    assert parse_cursor_value("timestamp with time zone", "2026-08-13T00:00:01+00:00") == (
        dt.datetime(2026, 8, 13, 0, 0, 1, tzinfo=dt.UTC)
    )


@pytest.mark.parametrize("cursor_type", ["text", "numeric", "character varying", "", "BIGINT"])
def test_a_type_outside_the_allowlist_is_refused(cursor_type: str) -> None:
    """`"BIGINT"` viết hoa nằm trong danh sách này CÓ CHỦ ĐÍCH: allowlist khớp
    chuỗi CHÍNH XÁC như `information_schema` trả về (chữ thường), và một phép
    so không phân biệt hoa thường sẽ lặng lẽ nhận cả `"TEXT"`."""
    with pytest.raises(CursorTypeNotAllowed):
        parse_cursor_value(cursor_type, "1")


def test_the_refusal_message_names_what_is_allowed() -> None:
    """Người đọc lỗi này là người vừa chọn nhầm cột làm watermark — họ cần biết
    chọn gì được, không chỉ biết mình sai."""
    with pytest.raises(CursorTypeNotAllowed, match="bigint"):
        parse_cursor_value("numeric", "1")


@pytest.mark.parametrize(
    ("cursor_type", "raw"),
    [
        ("bigint", "abc"),
        ("bigint", "400.0"),
        ("bigint", ""),
        ("date", "13/08/2026"),
        ("date", "2026-08-13T00:00:01"),
        ("timestamp without time zone", "hôm qua"),
    ],
)
def test_a_value_that_does_not_match_its_type_is_refused(cursor_type: str, raw: str) -> None:
    with pytest.raises(CursorValueUnusable):
        parse_cursor_value(cursor_type, raw)


def test_a_timestamptz_without_an_offset_is_refused() -> None:
    """Từ chối chứ KHÔNG đoán UTC — xem docstring `parse_cursor_value`.

    Đoán hộ làm watermark nhảy tới ±1 ngày, tức bỏ sót dữ liệu âm thầm. Từ chối
    để watermark nguyên vẹn và người gửi biết ngay phải sửa gì.
    """
    with pytest.raises(CursorValueUnusable, match="offset"):
        parse_cursor_value("timestamp with time zone", "2026-08-13T00:00:01")


def test_a_naive_timestamp_carrying_an_offset_is_refused() -> None:
    """Vế đối xứng. Trộn được hai dạng cho CÙNG một `cursor_type` là điều kiện
    đủ để `moves_forward` ném `TypeError` ("can't compare offset-naive and
    offset-aware datetimes") — một 500 ở đường báo tiến độ, xa hẳn nguyên
    nhân."""
    with pytest.raises(CursorValueUnusable, match="offset"):
        parse_cursor_value("timestamp without time zone", "2026-08-13T00:00:01+00:00")


def test_a_watermark_that_no_longer_parses_is_replaced_not_stuck() -> None:
    """Hàng `stream_state` sửa tay (hoặc khôi phục từ backup) không được khoá
    chết một stream: thay một watermark hỏng bằng một watermark đọc được là
    hướng đúng của hai lựa chọn — xem docstring `moves_forward`."""
    assert moves_forward("bigint", "khong-phai-so", "1000") is True


def test_a_candidate_that_does_not_parse_raises_instead_of_saying_no() -> None:
    """`candidate` là dữ liệu người gọi vừa gửi — từ chối request, KHÔNG lặng lẽ
    coi là "không tiến". Coi là "không tiến" thì pod nạp nhận 200 và tin rằng
    watermark đã được ghi nhận, trong khi nó không hề nhúc nhích."""
    with pytest.raises(CursorValueUnusable):
        moves_forward("bigint", "1000", "khong-phai-so")
