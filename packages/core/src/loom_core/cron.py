"""Nhịp kế tiếp của một lịch cron, có timezone, có luật DST tường minh."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import CroniterBadCronError, croniter


class CronInvalid(ValueError):
    """Biểu thức cron không đọc được."""


class TimezoneInvalid(ValueError):
    """Tên timezone không phải một vùng IANA."""


def _zone(timezone: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise TimezoneInvalid(f"timezone không dùng được: {timezone!r}") from exc


def next_tick(cron: str, timezone: str, after: datetime) -> datetime:
    """Mốc UTC của nhịp đầu tiên SAU `after`."""
    zone = _zone(timezone)
    local_after = after.astimezone(zone)
    try:
        it = croniter(cron, local_after)
    except (CroniterBadCronError, KeyError, ValueError) as exc:
        raise CronInvalid(f"cron không đọc được: {cron!r}") from exc

    local_next: datetime = it.get_next(datetime)

    # LUẬT 1 — giờ BIẾN MẤT (đồng hồ tiến)
    round_trip = local_next.astimezone(UTC).astimezone(zone)
    if round_trip.hour != local_next.hour or round_trip.minute != local_next.minute:
        return (local_next + timedelta(hours=1)).astimezone(UTC)

    # LUẬT 2 — giờ LẶP (đồng hồ lùi). Chọn fold=0 (lần đầu)
    first = local_next.replace(fold=0)
    return first.astimezone(UTC)
