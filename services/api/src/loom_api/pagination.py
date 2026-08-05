"""Cursor pagination trên khoá (updated_at, id).

`updated_at` MỘT MÌNH không duy nhất — nhiều item cùng mili giây là chuyện bình
thường khi import, và trong Postgres thì `now()` là thời điểm bắt đầu
TRANSACTION, nên cả một lô import đúng nghĩa dùng chung một giá trị. Cursor dựa
trên khoá không duy nhất thì lật trang sẽ nhảy hoặc lặp bản ghi, và không có gì
báo lỗi.
"""

import base64
import binascii
import hashlib
import json
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException, status


class CursorMismatch(HTTPException):
    def __init__(self) -> None:
        super().__init__(
            status.HTTP_400_BAD_REQUEST,
            "cursor không hợp lệ hoặc không thuộc bộ lọc hiện tại",
        )


def _filter_fingerprint(filters: dict[str, Any]) -> str:
    """Dấu vết của bộ lọc, không phụ thuộc thứ tự khoá. Ngắn — 16 hex là đủ để
    phát hiện nhầm lẫn, và cursor không phải nơi chống giả mạo (nó chỉ chứa
    updated_at và id, cả hai người dùng đã thấy)."""
    payload = json.dumps(
        {k: v for k, v in sorted(filters.items()) if v is not None},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def encode_cursor(updated_at: datetime, item_id: uuid.UUID, filters: dict[str, Any]) -> str:
    body = json.dumps(
        {
            # isoformat giữ microsecond. Mất nó là mất tính duy nhất của khoá.
            "t": updated_at.astimezone(UTC).isoformat(),
            "i": str(item_id),
            "f": _filter_fingerprint(filters),
        },
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(body.encode()).decode().rstrip("=")


def decode_cursor(token: str, filters: dict[str, Any]) -> tuple[datetime, uuid.UUID]:
    try:
        padded = token + "=" * (-len(token) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode()))
        if data["f"] != _filter_fingerprint(filters):
            raise CursorMismatch
        return datetime.fromisoformat(data["t"]), uuid.UUID(data["i"])
    except CursorMismatch:
        raise
    except (KeyError, ValueError, TypeError, binascii.Error, json.JSONDecodeError) as exc:
        # Cursor rác là lỗi của client (400), không phải lỗi server. Không bao giờ
        # để nó thành 500.
        raise CursorMismatch from exc


@dataclass
class Page:
    items: list[Any]
    next_cursor: str | None

    @classmethod
    def build(cls, rows: Sequence[Any], limit: int, cursor_of: Callable[[Any], str]) -> "Page":
        """Nhận limit+1 hàng và tự cắt.

        `has_more` suy ra từ số hàng lấy được, KHÔNG từ COUNT(*). Với bảng đã lọc
        quyền thì COUNT là một truy vấn đắt thứ hai cho mỗi trang, và nó vẫn không
        chính xác vì hàng có thể đổi giữa hai truy vấn."""
        if len(rows) > limit:
            kept = list(rows[:limit])
            return cls(items=kept, next_cursor=cursor_of(kept[-1]))
        return cls(items=list(rows), next_cursor=None)
