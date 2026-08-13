"""Watermark: kiểu cursor nào hợp lệ, và "tiến lên" nghĩa là gì cho từng kiểu.

**Vì sao so sánh watermark KHÔNG được là so sánh chuỗi.** `stream_state.
cursor_value` là `Text` (xem `loom_api.models.StreamState` — giá trị đi qua JSON
tới pod nạp rồi quay lại, và JSON không mang kiểu về). Nhưng "chuỗi" là cách
LƯU, không phải cách SO SÁNH. Đã kiểm trực tiếp bằng Python 3.12:

    sorted(["200", "400", "1000", "2000"]) -> ["1000", "200", "2000", "400"]
    "1000" > "400"                         -> False
    int("1000") > int("400")               -> True

Với một cursor `bigint` — trường hợp thường gặp nhất — một phép so `>` trên hai
`str` làm watermark KẸT VĨNH VIỄN ngay lần đầu giá trị vượt một mốc đổi số chữ
số: sau khi đã ghi `"1000"`, mọi giá trị `"2000"`, `"9999"` đều "lớn hơn" nhưng
`"10000"` thì KHÔNG, và từ đó mỗi lần nạp gia tăng đọc lại từ mốc cũ. Không lỗi
nào báo ra; triệu chứng là bản bronze phình lên vì trùng lặp không giới hạn.

Điều làm con bug này sống dai: timestamp ISO-8601 sắp xếp theo CHUỖI lại ĐÚNG
(`"2026-08-12T23:59:59" < "2026-08-13T00:00:01"`), và ba trong sáu kiểu hợp lệ
dưới đây là ngày/giờ. Nên một bản cài đặt so chuỗi xanh trên đúng một nửa số
kiểu, kể cả với một bộ test trông có vẻ đầy đủ. Phép so sánh phải đi qua
`parse_cursor_value` — nơi kiểu được khôi phục — chứ không bao giờ trên `str`.

**Nhà của danh sách kiểu là ĐÂY, không phải connector.** `loom_connector.
postgres` sinh ra nó (nó là bên duy nhất đọc `information_schema`), nhưng
`loom-api` phải kiểm ĐÚNG danh sách đó khi pod nạp báo `cursor_type` về, và
`loom-api` không phụ thuộc `loom-connector` (`services/api/pyproject.toml` —
kéo cả `pyarrow` + `psycopg` vào image control plane cho một `frozenset` sáu
chuỗi là cái giá sai). Cả hai bên phụ thuộc `loom-core`, nên nó ở đây và
connector import NGƯỢC LÊN — một bản chép thứ hai trôi được, và trôi ở đây
nghĩa là `loom-api` từ chối đúng cái `cursor_type` mà connector vừa đề xuất.

Tên kiểu khớp CHÍNH XÁC giá trị `information_schema.columns.data_type` của
Postgres trả về (chuỗi chuẩn SQL, có dấu cách — không phải tên rút gọn kiểu
`int4`/`timestamptz` của `pg_catalog`). Chúng mang dấu vết của Postgres vì
Postgres là nguồn duy nhất Giai đoạn 3a đọc được; một nguồn thứ hai sẽ phải ánh
xạ kiểu của nó về đúng những tên này, không phải mở rộng danh sách bằng phương
ngữ riêng.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Protocol

# Nguyên: so sánh SỐ HỌC, không tràn ở Python.
_INTEGER_CURSOR_TYPES = frozenset({"smallint", "integer", "bigint"})
_DATE_CURSOR_TYPE = "date"
# Hai kiểu timestamp tách riêng vì phép PARSE của chúng khác nhau, không chỉ vì
# tên khác nhau — xem `parse_cursor_value`.
_NAIVE_TIMESTAMP_CURSOR_TYPE = "timestamp without time zone"
_AWARE_TIMESTAMP_CURSOR_TYPE = "timestamp with time zone"

# NUMERIC và TEXT CỐ Ý bị loại, dù cả hai đều SO SÁNH ĐƯỢC ở Postgres:
#   - TEXT so sánh theo từ điển ("10" < "9" dạng chuỗi) — thứ tự chuỗi không
#     phải thứ tự thời gian chèn.
#   - NUMERIC so sánh đúng theo giá trị số, nhưng KHÔNG CÓ GÌ đảm bảo một dòng
#     mới hơn mang giá trị lớn hơn — một cột "amount" hoàn toàn có thể giảm dần
#     mà vẫn là NUMERIC hợp lệ.
# Một watermark có thể ĐI LÙI làm mất dữ liệu ÂM THẦM: khác với lỗi `>` so với
# `>=` (sinh trùng lặp — đếm được, khử được), một watermark lùi bỏ sót những
# dòng NẰM GIỮA giá trị cũ và giá trị mới thấp hơn, và không để lại dấu vết gì
# để nhận ra thiếu.
#
# Dựng bằng phép hợp của chính những hằng số mà `parse_cursor_value` phân
# nhánh trên, KHÔNG phải một `frozenset` sáu chuỗi viết tay: một kiểu nằm trong
# allowlist mà bộ phân tích không biết đọc sẽ lọt qua cổng 422 ở biên rồi nổ
# thành 500 ở tầng dưới. Xem `test_every_allowed_type_can_be_parsed`.
CURSOR_TYPE_ALLOWLIST: frozenset[str] = _INTEGER_CURSOR_TYPES | frozenset(
    {_DATE_CURSOR_TYPE, _NAIVE_TIMESTAMP_CURSOR_TYPE, _AWARE_TIMESTAMP_CURSOR_TYPE}
)


class CursorTypeNotAllowed(ValueError):
    """`cursor_type` nằm ngoài `CURSOR_TYPE_ALLOWLIST`.

    Tách khỏi `CursorValueUnusable` vì hai lỗi này sửa ở hai chỗ khác nhau: kiểu
    sai là chọn nhầm CỘT làm watermark (sửa ở cấu hình ingest), còn giá trị sai
    là pod nạp gửi lên một chuỗi không đúng kiểu nó tự khai (sửa ở pod). Gộp
    chung thành một `ValueError` trần thì người đọc log phải tự đoán.
    """


class CursorValueUnusable(ValueError):
    """`cursor_value` không đọc được dưới `cursor_type` đi kèm nó."""


class _Comparable(Protocol):
    """Đủ để `moves_forward` so sánh, và không hơn.

    `parse_cursor_value` trả về `int`, `date` hoặc `datetime` tuỳ kiểu; khai kiểu
    trả về là hợp `int | date | datetime` sẽ làm mypy strict từ chối `a < b` —
    đúng đắn, vì `int < date` và `date < datetime` đều ném `TypeError` ở runtime
    (đã kiểm: "can't compare datetime.datetime to datetime.date"). Protocol này
    nói ra điều mà lời gọi THẬT SỰ dựa vào: hai vế luôn tới từ CÙNG một
    `cursor_type`, nên chúng luôn cùng một kiểu cụ thể.
    """

    def __lt__(self, other: Any, /) -> bool: ...


def parse_cursor_value(cursor_type: str, raw: str) -> _Comparable:
    """Chuỗi đã lưu -> giá trị Python SO SÁNH ĐƯỢC. Ném, không bao giờ đoán.

    Hai kiểu timestamp KHẮT KHE về offset múi giờ, và đó là điều kiện để phép so
    sánh không bao giờ nổ: Python từ chối so một `datetime` naive với một
    `datetime` aware (`TypeError: can't compare offset-naive and offset-aware
    datetimes`). Nếu ở đây chấp nhận cả hai dạng cho cùng một `cursor_type` thì
    watermark cũ và giá trị mới có thể rơi vào hai dạng khác nhau, và lời gọi
    `moves_forward` biến thành một 500 ở đường báo tiến độ — xa hẳn chỗ gây ra
    (pod nạp định dạng thiếu offset).

    Lựa chọn còn lại là "thiếu offset thì coi như UTC". KHÔNG chọn nó: nó đoán
    hộ một thứ không suy ra được, và đoán sai làm watermark nhảy tới ±1 ngày —
    tức là bỏ sót dữ liệu ÂM THẦM, đúng loại hỏng mà cả module này tồn tại để
    chặn. Một `CursorValueUnusable` ồn ào (422 ở biên) để watermark NGUYÊN VẸN.
    """
    if cursor_type not in CURSOR_TYPE_ALLOWLIST:
        raise CursorTypeNotAllowed(
            f"cursor_type {cursor_type!r} is not usable as a watermark; "
            f"use one of {sorted(CURSOR_TYPE_ALLOWLIST)}"
        )

    if cursor_type in _INTEGER_CURSOR_TYPES:
        try:
            return int(raw)
        except ValueError as exc:
            raise CursorValueUnusable(
                f"cursor_value {raw!r} is not an integer, but cursor_type is {cursor_type!r}"
            ) from exc

    if cursor_type == _DATE_CURSOR_TYPE:
        try:
            return dt.date.fromisoformat(raw)
        except ValueError as exc:
            raise CursorValueUnusable(
                f"cursor_value {raw!r} is not an ISO-8601 date (YYYY-MM-DD)"
            ) from exc

    try:
        moment = dt.datetime.fromisoformat(raw)
    except ValueError as exc:
        raise CursorValueUnusable(f"cursor_value {raw!r} is not an ISO-8601 timestamp") from exc
    # `utcoffset()` chứ không chỉ `tzinfo is not None`: một `tzinfo` hợp lệ được
    # phép trả `None` cho offset (nó là một lớp trừu tượng, không phải một cờ),
    # và khi đó `datetime` vẫn được Python xếp là naive — đúng thuộc tính quyết
    # định việc so sánh có nổ hay không.
    is_aware = moment.utcoffset() is not None
    if cursor_type == _AWARE_TIMESTAMP_CURSOR_TYPE and not is_aware:
        raise CursorValueUnusable(
            f"cursor_value {raw!r} carries no UTC offset, but cursor_type is "
            f"{_AWARE_TIMESTAMP_CURSOR_TYPE!r}"
        )
    if cursor_type == _NAIVE_TIMESTAMP_CURSOR_TYPE and is_aware:
        raise CursorValueUnusable(
            f"cursor_value {raw!r} carries a UTC offset, but cursor_type is "
            f"{_NAIVE_TIMESTAMP_CURSOR_TYPE!r}"
        )
    return moment


def moves_forward(cursor_type: str, current: str, candidate: str) -> bool:
    """`candidate` có TIẾN LÊN so với `current` không, đọc dưới `cursor_type`.

    `<` chứ không `<=`: bằng nhau KHÔNG phải tiến. Ghi đè một watermark bằng
    chính nó chỉ tốn một `UPDATE` và làm `updated_at` nói dối rằng có tiến độ.

    **`current` không parse được thì trả `True`.** Không với tới được qua đường
    báo tiến độ (mọi giá trị ghi vào `stream_state` đều đã đi qua
    `parse_cursor_value` dưới đúng `cursor_type` được lưu kèm); nhánh này dành
    cho một hàng sửa tay hoặc khôi phục từ backup. Không có nó, một hàng như
    thế làm MỌI lần báo tiến độ của stream đó 500 vĩnh viễn và không có đường
    nào tự thoát ra — thay một watermark hỏng bằng một watermark đọc được là
    hướng đi ĐÚNG của hai lựa chọn.

    `candidate` không parse được thì NÉM ra ngoài, không nuốt: nó là dữ liệu
    người gọi vừa gửi, và câu trả lời đúng cho nó là từ chối request chứ không
    phải lặng lẽ coi là "không tiến".
    """
    parsed_candidate = parse_cursor_value(cursor_type, candidate)
    try:
        parsed_current = parse_cursor_value(cursor_type, current)
    except CursorValueUnusable:
        return True
    return parsed_current < parsed_candidate
