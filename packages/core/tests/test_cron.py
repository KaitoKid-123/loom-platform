"""Ba luật DST của lịch chạy — chốt ở spec mục 5.2, KHÔNG suy từ croniter.

`America/New_York` CÓ CHỦ ĐÍCH, không phải `Asia/Ho_Chi_Minh`: giờ Việt Nam
không có DST, nên một bộ test dùng nó sẽ XANH mà không kiểm được gì. Timezone
test phải là timezone có chuyển giờ thật, nếu không phép canh chỉ là trang trí.
"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from loom_core.cron import CronInvalid, TimezoneInvalid, next_tick

NY = "America/New_York"


def test_a_plain_daily_tick_lands_at_the_local_hour() -> None:
    after = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)
    got = next_tick("0 2 * * *", NY, after)
    assert got.astimezone(ZoneInfo(NY)).hour == 2


def test_a_repeated_hour_runs_once_at_the_FIRST_occurrence() -> None:
    """Đồng hồ LÙI 2026-11-01: 01:30 xảy ra hai lần ở New York.

    Cron `30 1 * * *` vì thế có hai mốc UTC hợp lệ (05:30Z và 06:30Z). Luật:
    chạy MỘT lần, ở lần ĐẦU — mốc UTC sớm hơn.
    """
    after = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
    got = next_tick("30 1 * * *", NY, after)
    assert got == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)


def test_a_missing_hour_runs_at_the_next_valid_instant_not_skipped() -> None:
    """Đồng hồ TIẾN 2026-03-08: 02:30 KHÔNG TỒN TẠI ở New York.

    Luật: chạy ở mốc hợp lệ kế tiếp (03:00 địa phương = 07:00Z), KHÔNG bỏ cả ngày.
    """
    after = datetime(2026, 3, 8, 0, 0, tzinfo=UTC)
    got = next_tick("30 2 * * *", NY, after)
    assert got == datetime(2026, 3, 8, 7, 0, tzinfo=UTC)


def test_a_bad_cron_is_refused_at_the_edge() -> None:
    with pytest.raises(CronInvalid):
        next_tick("khong phai cron", NY, datetime(2026, 1, 1, tzinfo=UTC))


def test_a_bad_timezone_is_refused_at_the_edge() -> None:
    with pytest.raises(TimezoneInvalid):
        next_tick("0 2 * * *", "Mars/Olympus", datetime(2026, 1, 1, tzinfo=UTC))


def test_the_returned_instant_is_always_utc_and_aware() -> None:
    got = next_tick("0 2 * * *", NY, datetime(2026, 6, 1, tzinfo=UTC))
    assert got.tzinfo is UTC
