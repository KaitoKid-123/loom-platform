"""Điểm vào: dựng client HTTP, bắt SIGTERM, chạy vòng lặp cho tới khi được bảo dừng.

KHÔNG có `create_app()` và không có server nào — khác `loom_api.main` và
`loom_query.main`. Tiến trình này không NHẬN request; nó chỉ gửi đi. Hệ quả
nhìn thấy được ở chart: `scheduler-deployment.yaml` không có `ports`, không có
Service, và không có probe HTTP nào (xem docstring ở đó cho vì sao KHÔNG dựng
một cổng `/healthz` chỉ để làm vui lòng kubelet).

## Vì sao SIGTERM phải được bắt tường minh

Mặc định của Python là chết ngay ở SIGTERM, không chạy `finally`, không đóng
`httpx.AsyncClient`. Với một tiến trình chỉ ngủ và POST thì mất mát vật chất là
gần bằng không — nhưng cái mất là khả năng PHÂN BIỆT: một pod bị SIGKILL sau
`terminationGracePeriodSeconds` và một pod tự thoát sạch đều để lại "container
đã dừng", còn `exit 0` kèm một dòng log `shutdown` thì nói rõ nó đã nghe thấy
tín hiệu. Ở một thành phần mà triệu chứng hỏng là "không có gì xảy ra cả", mọi
tín hiệu phân biệt được đều đáng giữ.

`loop.add_signal_handler` chứ không `signal.signal`: handler của `signal.signal`
chạy giữa hai bytecode của luồng chính, và đặt một `asyncio.Event` từ đó KHÔNG
đánh thức event loop đang chờ trong `epoll` — vòng lặp sẽ chỉ nhận ra cờ đã đổi
ở lần tỉnh kế tiếp, tức là tới `max_backoff_seconds` sau. `add_signal_handler`
đi qua đường tự-đánh-thức của event loop nên `_sleep_or_stop` tỉnh ngay.
"""

from __future__ import annotations

import asyncio
import logging
import signal

import httpx
import structlog

from loom_scheduler.config import Settings, get_settings
from loom_scheduler.ticker import run_ticker, tick_url

log = structlog.get_logger("loom_scheduler.main")


def configure_logging(level: str = "INFO") -> None:
    """Log JSON ra stdout — bản sao gọn của `loom_api.logging.configure_logging`.

    Bản sao chứ không dùng chung: `loom_api` là một service KHÁC, và service
    này cố ý chỉ phụ thuộc đúng MỘT module của `loom-core`
    (`loom_core.internal_auth`, xem `tests/test_no_db_no_k8s.py`). Kéo cả
    `loom-api` vào để dùng lại mười dòng cấu hình log sẽ kéo theo SQLAlchemy,
    Alembic và client Kubernetes — tức là phá cả hai tính chất mà phép canh kia
    tồn tại để giữ.
    """
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", level=numeric_level, force=True)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        cache_logger_on_first_use=False,
    )


def install_stop_handlers(loop: asyncio.AbstractEventLoop, stop: asyncio.Event) -> None:
    """SIGTERM (Kubernetes) và SIGINT (Ctrl-C khi chạy tay) đều chỉ ĐẶT CỜ.

    Không `sys.exit`, không huỷ task: `run_ticker` tự thoát ở lần kiểm cờ kế
    tiếp — và vì nó chờ bằng `asyncio.wait_for(stop.wait())`, "kế tiếp" là ngay
    lập tức chứ không phải sau một chu kỳ. Xem `ticker._sleep_or_stop`.
    """
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)


async def run(settings: Settings) -> None:
    stop = asyncio.Event()
    install_stop_handlers(asyncio.get_running_loop(), stop)
    log.info(
        "scheduler_started",
        url=tick_url(settings),
        tick_seconds=settings.tick_seconds,
        environment=settings.environment,
    )
    async with httpx.AsyncClient() as client:
        ticks = await run_ticker(settings, client, stop=stop)
    log.info("scheduler_stopped", ticks=ticks)


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    asyncio.run(run(settings))


if __name__ == "__main__":
    main()
