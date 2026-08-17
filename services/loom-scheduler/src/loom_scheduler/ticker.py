"""Vòng lặp: ngủ → POST `/internal/schedule/tick` → ghi log. Không gì khác.

**Service này KHÔNG ra quyết định nào.** Nó không biết cron là gì, không biết
pipeline nào tới hạn, không đọc `pipeline_run`. Mọi quyết định — lịch nào tới
hạn, run nào được khởi động hay bị bỏ qua, bước nào đã xong — nằm ở
`loom_api.routers.internal_schedule` và ở `decide()`, nơi chúng có database để
đứng cạnh và một ràng buộc `UNIQUE (pipeline_id, scheduled_for)` để chốt tính
idempotent. Một bản sao của bất kỳ luật nào trong số đó ở đây sẽ là một luật
thứ hai phải giữ khớp bằng trí nhớ, và nó sẽ trôi.

## Vì sao một lỗi HTTP KHÔNG được làm chết vòng lặp

Đây là tính chất quan trọng nhất của file này. `loom-scheduler` là nhịp tim của
CẢ nền tảng: nếu nó chết, không một pipeline nào chạy — không phải pipeline gây
ra lỗi, mà TẤT CẢ. Và những nguyên nhân khiến một tick hỏng lại đúng là những
nguyên nhân tạm thời nhất có thể: `loom-api` đang rollout (503 vài giây),
DNS chưa sẵn sàng lúc cụm mới lên, một tick chạm giới hạn thời gian của chính
nó. Chết vì một trong số đó là đổi một sự cố 5 giây lấy một sự cố kéo dài tới
khi có người nhìn vào cụm.

Nên MỌI `Exception` đều bị bắt và ghi log, không chỉ `httpx.HTTPError`.
`asyncio.CancelledError` KHÔNG bị bắt (nó kế thừa `BaseException` từ Python
3.8), nên việc tắt êm ở `main.py` vẫn dừng được vòng lặp này ngay lập tức.

## Backoff, và vì sao nó có TRẦN

Một `loom-api` sập mà scheduler vẫn gõ cửa mỗi 30 giây là một dòng log rác mỗi
30 giây và một lượt kết nối vô ích; nhân đôi khoảng chờ sau mỗi lần hỏng liên
tiếp làm phần đó im đi. Nhưng nhân đôi KHÔNG GIỚI HẠN thì một sự cố nửa giờ để
lại một scheduler còn ngủ hàng giờ SAU KHI mọi thứ đã sống lại — và mọi nhịp
cron trong khoảng ngủ thừa đó trôi qua không ai xử lý. Vì vậy
`max_backoff_seconds`, và vì vậy bộ đếm được đặt lại về 0 ngay lần tick thành
công đầu tiên.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import structlog

from loom_core.internal_auth import SCHEDULE_SHARED_SECRET_HEADER
from loom_scheduler.config import Settings

log = structlog.get_logger("loom_scheduler.ticker")

# Số lần hỏng liên tiếp mà backoff còn nhân đôi. Ghim ở đây chứ không đưa ra
# `Settings`: nó không phải một tham số vận hành — `max_backoff_seconds` mới là
# thứ người vận hành cần chỉnh, và hai núm cho cùng một hành vi chỉ mời người
# ta chỉnh nhầm núm.
_MAX_DOUBLINGS = 6


def tick_url(settings: Settings) -> str:
    """Nơi DUY NHẤT trong repo này biết đường dẫn của endpoint tick.

    Xem `Settings.api_base_url`: cấu hình mang GỐC của loom-api, phần đường dẫn
    ở đây. Một hàm chứ không một hằng số nối sẵn để test khẳng định được đúng
    chuỗi mà vòng lặp gọi, không phải một chuỗi chép lại.
    """
    return f"{settings.api_base_url}/internal/schedule/tick"


def _delay_seconds(settings: Settings, consecutive_failures: int) -> float:
    """Khoảng ngủ trước lần POST kế tiếp — xem docstring đầu file cho lý do có trần."""
    if consecutive_failures <= 0:
        return settings.tick_seconds
    # `float(...)` tường minh: `2 ** n` cho mypy một `Any` (toán tử `**` có
    # overload trả `int` hoặc `float` tuỳ dấu số mũ), và một `Any` lọt vào đây
    # sẽ lặng lẽ tắt kiểm kiểu cho cả phép tính nhịp.
    factor = float(2 ** min(consecutive_failures, _MAX_DOUBLINGS))
    return min(settings.tick_seconds * factor, settings.max_backoff_seconds)


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> bool:
    """Ngủ `seconds`, HOẶC tỉnh ngay khi `stop` được đặt. `True` nghĩa là đã được yêu cầu dừng.

    `asyncio.wait_for(stop.wait(), ...)` chứ không `asyncio.sleep(...)` rồi mới
    kiểm cờ: một `sleep` không cắt được nghĩa là SIGTERM phải chờ hết chu kỳ
    hiện tại — tới `max_backoff_seconds` = 5 phút khi đang backoff — trong khi
    `terminationGracePeriodSeconds` của pod là 30 giây. Quá hạn đó Kubernetes
    gửi SIGKILL, và "tắt êm" trở thành một câu trong tài liệu chứ không phải
    một tính chất.
    """
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        return False
    return True


def _summary(response: httpx.Response) -> dict[str, Any]:
    """Bốn con số của `TickResponse`, hoặc một mô tả đủ để đi tìm nếu không đọc được.

    KHÔNG để `response.json()` ném ra ngoài: một `loom-api` trả HTML (ingress
    chen vào giữa, chẳng hạn) thì đó là chuyện đáng ghi log, không phải chuyện
    đáng tính là một lần tick HỎNG — request đã 2xx, và bộ đếm backoff không
    nên tăng vì một lỗi phía người đọc.
    """
    try:
        payload = response.json()
    except ValueError:
        return {"body": response.text[:200]}
    if not isinstance(payload, dict):
        return {"body": str(payload)[:200]}
    return {str(k): v for k, v in payload.items()}


async def run_ticker(
    settings: Settings,
    client: httpx.AsyncClient,
    *,
    stop: asyncio.Event,
    max_ticks: int | None = None,
) -> int:
    """Ngủ, POST, ghi log — cho tới khi `stop` được đặt. Trả về số lần đã POST.

    NGỦ TRƯỚC, POST SAU, và thứ tự đó có lý do: pod này khởi động cùng lúc với
    `loom-api` trong một lần `helm upgrade`, nên một POST ngay ở giây thứ 0 gần
    như chắc chắn rơi vào lúc bên kia chưa Ready. Một dòng WARN ở mỗi lần triển
    khai là loại nhiễu làm người ta ngừng đọc log.

    `max_ticks` CHỈ cho test: nó cho một phép kiểm chạy vòng lặp thật (cùng mã
    mà production chạy) rồi dừng, thay vì phải chép lại thân vòng lặp trong
    test. Production không truyền tham số này — `main.py` dừng bằng `stop`.
    """
    url = tick_url(settings)
    headers = {SCHEDULE_SHARED_SECRET_HEADER: settings.shared_secret}
    consecutive_failures = 0
    ticks = 0

    while not stop.is_set():
        if max_ticks is not None and ticks >= max_ticks:
            return ticks
        if await _sleep_or_stop(stop, _delay_seconds(settings, consecutive_failures)):
            return ticks

        ticks += 1
        try:
            response = await client.post(
                url, headers=headers, timeout=settings.request_timeout_seconds
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            consecutive_failures += 1
            log.warning(
                "tick_rejected",
                url=url,
                status_code=exc.response.status_code,
                # Thân phản hồi CẮT NGẮN: 401 từ `require_schedule_secret` là
                # nguyên nhân thường gặp nhất ở đây (bí mật lệch giữa chart và
                # pod), và câu "missing or invalid schedule secret" chỉ thẳng
                # vào đó — một dòng log chỉ có mã 401 thì không.
                body=exc.response.text[:200],
                consecutive_failures=consecutive_failures,
            )
        # `Exception` TRẦN, có chủ ý — xem "Vì sao một lỗi HTTP KHÔNG được làm
        # chết vòng lặp" ở đầu file. `asyncio.CancelledError` kế thừa
        # `BaseException` nên nó KHÔNG rơi vào đây, và việc tắt êm vẫn dừng
        # được vòng lặp ngay lập tức.
        except Exception as exc:
            consecutive_failures += 1
            log.warning(
                "tick_failed",
                url=url,
                error=type(exc).__name__,
                detail=str(exc)[:200],
                consecutive_failures=consecutive_failures,
            )
        else:
            consecutive_failures = 0
            log.info("tick_ok", url=url, **_summary(response))

    return ticks
