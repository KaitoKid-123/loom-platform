"""Logic thuần quyết định có chạy một tick hay bỏ nhịp.

Không database, không HTTP — thuần để test được dễ dàng.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal


@dataclass(frozen=True, slots=True)
class Decision:
    """`reason` chỉ có nghĩa khi `action == "skip"`, và nó đi THẲNG vào
    `pipeline_run.skip_reason` — tức là nó là thứ người vận hành đọc lúc 9 giờ
    sáng để hiểu vì sao đêm qua không chạy. Viết cho người đó, không phải cho log."""

    action: Literal["start", "skip"]
    reason: str = ""


def decide(
    *,
    due_at: datetime,
    has_active_run: bool,
    active_run_started_at: datetime | None,
    concurrent_runs: int,
    concurrency_cap: int,
) -> Decision:
    # Chốt 1 — không tự giẫm.
    if has_active_run:
        started = active_run_started_at.isoformat() if active_run_started_at else "?"
        return Decision("skip", f"run trước còn đang chạy từ {started}")
    # Chốt 2 — trần toàn cục. Bỏ nhịp chứ KHÔNG xếp hàng.
    if concurrent_runs >= concurrency_cap:
        return Decision("skip", f"đã đụng trần đồng thời {concurrent_runs}/{concurrency_cap}")
    return Decision("start")
