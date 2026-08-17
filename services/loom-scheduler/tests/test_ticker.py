"""Vòng lặp, backoff, tắt êm — ba tính chất, và tính chất giữa là tính chất đắt nhất.

Mọi phép kiểm ở đây chạy CHÍNH `run_ticker` mà production chạy, qua một
`httpx.MockTransport`: client là `httpx.AsyncClient` thật, nên `raise_for_status`,
việc dựng header và việc ghép URL đều là mã thật chứ không phải một bản mô phỏng.
"""

from __future__ import annotations

import asyncio
import os
import signal
import time

import httpx
import pytest
import structlog

from loom_core.internal_auth import SCHEDULE_SHARED_SECRET_HEADER
from loom_scheduler import ticker
from loom_scheduler.config import Settings
from loom_scheduler.main import install_stop_handlers
from loom_scheduler.ticker import _delay_seconds, run_ticker, tick_url


# Nhịp NHỎ để test không phải chờ thật; các phép kiểm nào cần quan sát ĐỘ DÀI
# của nhịp thì thay `_sleep_or_stop` bằng một bản ghi lại (xem
# `test_the_failure_counter_resets_after_a_good_tick`) chứ không đo đồng hồ —
# một phép kiểm backoff dựa vào wall clock là một phép kiểm sẽ chớp đỏ trên CI.
def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "api_base_url": "http://loom-api.test:8000",
        "shared_secret": "s3cret",
        "tick_seconds": 0.001,
        "max_backoff_seconds": 0.01,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _client(handler: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


_OK_BODY = {"schedules_processed": 2, "runs_started": 1, "runs_skipped": 1, "runs_failed": 0}


# ------------------------------------------------------------------ địa chỉ + header


def test_the_tick_url_is_the_api_root_plus_the_internal_path() -> None:
    assert tick_url(_settings()) == "http://loom-api.test:8000/internal/schedule/tick"


def test_a_trailing_slash_in_the_base_url_does_not_become_a_double_slash() -> None:
    """`http://host//internal/...` là một đường dẫn KHÁC với Starlette — 404 mỗi N giây."""
    assert tick_url(_settings(api_base_url="http://loom-api.test:8000/")) == (
        "http://loom-api.test:8000/internal/schedule/tick"
    )


async def test_the_loop_posts_to_the_tick_endpoint_with_the_shared_secret() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_OK_BODY)

    settings = _settings()
    async with _client(handler) as client:
        ticks = await run_ticker(settings, client, stop=asyncio.Event(), max_ticks=2)

    assert ticks == 2
    assert [r.method for r in seen] == ["POST", "POST"]
    assert [str(r.url) for r in seen] == [tick_url(settings)] * 2
    assert seen[0].headers[SCHEDULE_SHARED_SECRET_HEADER] == "s3cret"


# ------------------------------------------------------------------ lỗi không giết vòng lặp


async def test_an_http_error_status_does_not_kill_the_loop() -> None:
    """503 giữa một lần rollout của `loom-api` KHÔNG được dừng cả nền tảng.

    Đây là tính chất mà file `ticker.py` tồn tại để giữ: nếu cái đồng hồ chết,
    không một pipeline nào chạy — không phải pipeline gây lỗi, mà TẤT CẢ.
    """
    codes = [503, 500, 401, 200]
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        code = codes[len(calls)]
        calls.append(code)
        if code == 200:
            return httpx.Response(200, json=_OK_BODY)
        return httpx.Response(code, json={"detail": "missing or invalid schedule secret"})

    async with _client(handler) as client:
        ticks = await run_ticker(_settings(), client, stop=asyncio.Event(), max_ticks=4)

    assert ticks == 4
    assert calls == codes


async def test_a_transport_error_does_not_kill_the_loop() -> None:
    """DNS chưa sẵn sàng lúc cụm mới lên là một `httpx.ConnectError`, không phải một status.

    `raise_for_status` không bao giờ được gọi trong trường hợp này — lỗi ném ra
    từ chính `client.post`. Một `except httpx.HTTPStatusError` đơn độc sẽ để lỗi
    này giết vòng lặp.
    """
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) <= 2:
            raise httpx.ConnectError("nodename nor servname provided", request=request)
        return httpx.Response(200, json=_OK_BODY)

    async with _client(handler) as client:
        ticks = await run_ticker(_settings(), client, stop=asyncio.Event(), max_ticks=3)

    assert ticks == 3
    assert len(calls) == 3


async def test_an_unexpected_exception_does_not_kill_the_loop() -> None:
    """Không chỉ lỗi `httpx`. Bất kỳ `Exception` nào cũng phải bị nuốt và ghi log.

    Xem docstring `ticker`: một `RuntimeError` từ tầng nào đó của client vẫn là
    một sự cố tạm thời so với hậu quả của việc mọi lịch trong nền tảng ngừng
    chạy tới khi có người nhìn vào cụm.
    """
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("một thứ không ai lường trước")
        return httpx.Response(200, json=_OK_BODY)

    async with _client(handler) as client:
        ticks = await run_ticker(_settings(), client, stop=asyncio.Event(), max_ticks=2)

    assert ticks == 2


async def test_a_body_that_is_not_json_still_counts_as_a_successful_tick() -> None:
    """2xx + thân không đọc được là chuyện của người ĐỌC log, không phải một tick hỏng.

    Quan sát qua độ dài nhịp KẾ TIẾP: nếu nó thành `tick_seconds * 2` thì bộ
    đếm hỏng đã tăng, tức là một `loom-api` trả HTML (ingress chen vào giữa) sẽ
    đẩy scheduler vào backoff dù nó đã 200.
    """
    delays: list[float] = []

    async def fake_sleep(stop: asyncio.Event, seconds: float) -> bool:
        delays.append(seconds)
        return False

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not the api you asked for</html>")

    settings = _settings(tick_seconds=10.0, max_backoff_seconds=1000.0)
    original = ticker._sleep_or_stop
    ticker._sleep_or_stop = fake_sleep  # type: ignore[assignment]
    try:
        async with _client(handler) as client:
            await run_ticker(settings, client, stop=asyncio.Event(), max_ticks=2)
    finally:
        ticker._sleep_or_stop = original  # type: ignore[assignment]

    assert delays == [10.0, 10.0]


# ------------------------------------------------------------------ backoff


def test_backoff_doubles_on_consecutive_failures_and_stops_at_the_ceiling() -> None:
    settings = _settings(tick_seconds=10.0, max_backoff_seconds=100.0)
    assert _delay_seconds(settings, 0) == 10.0
    assert _delay_seconds(settings, 1) == 20.0
    assert _delay_seconds(settings, 2) == 40.0
    assert _delay_seconds(settings, 3) == 80.0
    # Trần cắt vào từ đây — 10 * 2**4 = 160 > 100.
    assert _delay_seconds(settings, 4) == 100.0
    assert _delay_seconds(settings, 5) == 100.0


def test_backoff_never_grows_without_bound() -> None:
    """Trần là tính chất, không phải một chi tiết cài đặt.

    Không có nó, một sự cố nửa giờ để lại một scheduler còn ngủ HÀNG GIỜ sau
    khi mọi thứ đã sống lại, và mọi nhịp cron trong khoảng ngủ thừa đó trôi qua
    không ai xử lý. `2**1000` là một số Python biểu diễn được, nên "nó sẽ tràn"
    không phải một hàng rào.
    """
    settings = _settings(tick_seconds=30.0, max_backoff_seconds=300.0)
    assert _delay_seconds(settings, 1000) == 300.0


async def test_the_failure_counter_resets_after_a_good_tick() -> None:
    """Nhịp phải TRỞ VỀ `tick_seconds` ngay lần tick thành công đầu tiên.

    Quan sát chuỗi độ dài nhịp mà chính vòng lặp yêu cầu, thay vì đo đồng hồ —
    một phép kiểm backoff dựa vào wall clock sẽ chớp đỏ trên CI.
    """
    delays: list[float] = []

    async def fake_sleep(stop: asyncio.Event, seconds: float) -> bool:
        delays.append(seconds)
        return False

    codes = [500, 500, 200, 500]
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        code = codes[len(calls)]
        calls.append(code)
        if code == 200:
            return httpx.Response(200, json=_OK_BODY)
        return httpx.Response(code, json={"detail": "boom"})

    settings = _settings(tick_seconds=10.0, max_backoff_seconds=1000.0)
    original = ticker._sleep_or_stop
    ticker._sleep_or_stop = fake_sleep  # type: ignore[assignment]
    try:
        async with _client(handler) as client:
            await run_ticker(settings, client, stop=asyncio.Event(), max_ticks=4)
    finally:
        ticker._sleep_or_stop = original  # type: ignore[assignment]

    # nhịp 1: chưa hỏng lần nào -> 10
    # nhịp 2: một lần hỏng      -> 20
    # nhịp 3: hai lần hỏng      -> 40
    # nhịp 4: vừa thành công    -> 10   <- chỗ phép kiểm này canh
    assert delays == [10.0, 20.0, 40.0, 10.0]


# ------------------------------------------------------------------ tắt êm


async def test_stopping_ends_the_loop_without_waiting_out_the_interval() -> None:
    """Cờ dừng phải CẮT NGANG nhịp đang ngủ, không phải chờ hết nhịp.

    `tick_seconds=30` ở đây là cố ý: nếu `_sleep_or_stop` ngủ bằng
    `asyncio.sleep` rồi mới kiểm cờ, phép kiểm này treo 30 giây rồi đỏ vì quá
    hạn — đúng cái sẽ xảy ra trên cụm, chỉ khác là ở đó Kubernetes gửi SIGKILL
    sau `terminationGracePeriodSeconds` và không ai thấy gì.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_OK_BODY)

    stop = asyncio.Event()
    settings = _settings(tick_seconds=30.0, max_backoff_seconds=30.0)
    async with _client(handler) as client:
        started = time.monotonic()
        task = asyncio.create_task(run_ticker(settings, client, stop=stop))
        await asyncio.sleep(0.05)
        stop.set()
        ticks = await asyncio.wait_for(task, timeout=5.0)
        elapsed = time.monotonic() - started

    assert ticks == 0
    assert elapsed < 5.0


async def test_sigterm_stops_the_loop_within_one_tick_interval() -> None:
    """Tín hiệu THẬT, không phải một `stop.set()` gọi tay.

    `install_stop_handlers` dùng `loop.add_signal_handler`, và lý do nằm ở
    docstring `main.py`: một handler kiểu `signal.signal` đặt được cờ nhưng
    KHÔNG đánh thức event loop đang chờ trong `epoll`, nên vòng lặp chỉ nhận ra
    ở lần tỉnh kế tiếp — tới `max_backoff_seconds` sau. Chỉ một tín hiệu thật
    mới phân biệt được hai cách cài đặt đó.

    An toàn cho tiến trình pytest: handler được cài TRƯỚC khi `os.kill` chạy
    (`add_signal_handler` ném lỗi nếu không cài được, chứ không im lặng), và
    `finally` gỡ nó ra để không ảnh hưởng test sau.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_OK_BODY)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    install_stop_handlers(loop, stop)
    settings = _settings(tick_seconds=30.0, max_backoff_seconds=30.0)
    try:
        async with _client(handler) as client:
            started = time.monotonic()
            task = asyncio.create_task(run_ticker(settings, client, stop=stop))
            await asyncio.sleep(0.05)
            os.kill(os.getpid(), signal.SIGTERM)
            ticks = await asyncio.wait_for(task, timeout=5.0)
            elapsed = time.monotonic() - started
    finally:
        loop.remove_signal_handler(signal.SIGTERM)
        loop.remove_signal_handler(signal.SIGINT)

    assert stop.is_set()
    assert ticks == 0
    # "Trong một chu kỳ tick" — chu kỳ ở đây là 30 giây, và nó phải về nhanh hơn
    # thế RẤT nhiều, không chỉ đúng dưới trần.
    assert elapsed < 5.0


# ------------------------------------------------------------------ cấu hình


def test_the_default_secret_is_refused_outside_local() -> None:
    """Cùng phép kiểm mà `loom-api` và `loom-query` mỗi bên tự giữ một bản.

    Giá trị mặc định PHẢI khớp `loom_core.config` — không khớp thì một bên từ
    chối khởi động còn bên kia không, và cụm chỉ nói ra sự lệch đó dưới dạng
    401 ở lần tick đầu tiên trong production.
    """
    with pytest.raises(ValueError, match="shared_secret"):
        Settings(environment="prod")

    ok = Settings(environment="prod", shared_secret="một bí mật thật")
    assert ok.shared_secret == "một bí mật thật"


def test_a_non_positive_tick_interval_is_refused() -> None:
    """`tick_seconds=0` là một vòng lặp bận quay hết một CPU và nện `loom-api`."""
    with pytest.raises(ValueError, match="tick_seconds"):
        Settings(tick_seconds=0)


# ------------------------------------------------------------------ log


async def test_a_successful_tick_logs_the_counters_it_got_back() -> None:
    """Log là giao diện DUY NHẤT của service này — không có endpoint, không có metric.

    Nếu `runs_started` không đi vào dòng log thì câu hỏi "lịch có chạy không"
    chỉ trả lời được bằng cách đọc database, và đó là đúng thứ mà một service
    "không database" không giúp được gì.
    """
    captured: list[dict[str, object]] = []

    def sink(_logger: object, _name: str, event_dict: dict[str, object]) -> str:
        captured.append(dict(event_dict))
        return ""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_OK_BODY)

    structlog.configure(processors=[sink], cache_logger_on_first_use=False)
    try:
        async with _client(handler) as client:
            await run_ticker(_settings(), client, stop=asyncio.Event(), max_ticks=1)
    finally:
        structlog.reset_defaults()

    ok = [e for e in captured if e.get("event") == "tick_ok"]
    assert len(ok) == 1
    assert ok[0]["runs_started"] == 1
    assert ok[0]["schedules_processed"] == 2
