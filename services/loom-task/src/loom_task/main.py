"""Điểm vào của pod nạp: chạy MỘT lần, đóng run, rồi chết.

**Mọi đường ra khỏi `ingest` đi qua `complete`** — kể cả một ngoại lệ không ai
lường trước, xem `run_reporting_the_outcome`. Đó là điều làm cột
`ingest_run.status` nói đúng sự thật cho người đọc bảng thay vì để một run nằm
lại ở `running` chỉ vì mã này thiếu một nhánh `except`.

**Nói cho đúng phạm vi, vì câu trên rất dễ đọc thành một bảo đảm mà nó không
phải.** BA lớp trường hợp KHÔNG được nó phủ:

1. Hỏng TRƯỚC khi có client — `Settings()` thiếu biến môi trường, hoặc chính lời
   gọi dựng `IngestClient` ném. Lúc đó không có `run_id` lẫn bí mật để nói với
   ai, nên không báo được gì. (Đây chính là lý do `SourceCredentials` là một lớp
   RIÊNG — xem `config.py`: credential nguồn thiếu là lỗi thường gặp nhất trong
   nhóm này, và tách ra thì nó rơi vào vùng BÁO ĐƯỢC.)
2. SIGKILL — OOMKill, node mất điện. Không `except` nào chạy.
3. `loom-api` không với tới được: lời báo hỏng cũng hỏng (`main.py` log lại rồi
   ném tiếp lỗi gốc, xem `run_reporting_the_outcome`).

Thứ phủ cả ba là vòng đối chiếu của Task 13 (`job_name` tất định +
`JobLauncher.status()` cho biết Job đã chết mà run vẫn `running`), không phải
khối `try` dưới đây. Cái `try` này chỉ mua một thứ, và mua thật: những lỗi mà
tiến trình CÒN SỐNG để kể lại thì được kể lại ngay, kèm lý do.

**Đường ghi bronze (Iceberg) CHƯA được nối ở Task 11** — xem `_build_sink`. Vòng
lặp `incremental` và hợp đồng thứ tự của nó đã xong và đã có phép canh; thứ còn
thiếu là một `Sink` thật, và nó thuộc Task 12 (thiết kế staging-rồi-tráo-tên, sau
ĐO 2). Cho tới lúc đó ảnh này chạy được, khởi động được, và ĐÓNG run bằng
`failed` kèm đúng lý do đó — không phải kẹt ở `running`, và không phải một lần
nạp "thành công" không ghi gì.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from urllib.parse import quote

import structlog

from loom_connector import Connector
from loom_connector.postgres import PostgresConnector
from loom_core.schemas import IngestSourceSpec, IngestSpec
from loom_task.client import IngestClient, IngestClientLike
from loom_task.config import Settings, SourceCredentials
from loom_task.runner import Sink, resolve_cursor, run_incremental

logger = structlog.get_logger(__name__)

# `IngestCompletionReport.error` giới hạn 2000 ký tự (xem schema). Cắt Ở ĐÂY chứ
# không để pydantic từ chối: một traceback dài làm request `complete` thành 422,
# và hậu quả là run kẹt ở `running` VÌ đã hỏng — hai lỗi cộng lại thành một trạng
# thái không ai giải thích được.
_ERROR_LIMIT = 2000


class SourceUnreachable(RuntimeError):
    """`connector.check()` trả về thất bại. Watermark KHÔNG đổi (spec mục 7)."""


class SourceKindNotSupported(RuntimeError):
    """Giai đoạn 3a chỉ đọc Postgres — `ConnectionDefinition.kind` cho phép
    `mysql`/`sqlserver`/`rest` từ Giai đoạn 1, nhưng không có connector nào cho
    chúng. Hỏng ồn ào thay vì thử một DSN Postgres tới một cổng MySQL."""


class ModeNotBuiltYet(RuntimeError):
    """`mode="full"` là Task 12 (bảng tạm rồi tráo tên, sau ĐO 2)."""


class SinkNotBuiltYet(RuntimeError):
    """Đường ghi bronze chưa được nối — xem docstring module và `_build_sink`."""


def _source_dsn(source: IngestSourceSpec, credentials: SourceCredentials) -> str:
    """DSN của NGUỒN, ghép từ spec (host/port/db) và Secret của pod (user/mật khẩu).

    `quote(..., safe="")` cho cả tên đăng nhập và mật khẩu: một mật khẩu chứa
    `@`, `/`, `:` hay `?` mà đi thẳng vào URI sẽ CẮT chuỗi ở đúng ký tự đó, và
    libpq đọc ra một host khác — triệu chứng là "could not translate host name",
    một câu không nhắc gì tới mật khẩu, nên nó gửi người đọc log đi sai hướng
    hoàn toàn. Phần trăm-mã-hoá là cách libpq quy định để đưa ký tự đặc biệt vào
    một URI kết nối.

    `database` bắt buộc cho Postgres: thiếu nó libpq lấy tên database bằng tên
    đăng nhập, tức là nạp từ MỘT database khác cái người dùng đã chọn — và mọi
    thứ vẫn "chạy".
    """
    if source.kind != "postgres":
        raise SourceKindNotSupported(
            f"connection kind {source.kind!r} chưa có connector — Giai đoạn 3a chỉ đọc 'postgres'"
        )
    if not source.database:
        raise SourceKindNotSupported(
            "connection postgres không khai `database` — libpq sẽ nối vào một "
            "database mang tên người dùng, không phải database đã chọn"
        )
    user = quote(credentials.source_user, safe="")
    password = quote(credentials.source_password, safe="")
    database = quote(source.database, safe="")
    return f"postgresql://{user}:{password}@{source.host}:{source.port}/{database}"


def _build_sink(spec: IngestSpec) -> Sink:
    """CHƯA có bản cài đặt. Task 12 nối `loom_iceberg.Lakehouse` vào đây.

    Không trả về một sink "tạm" ghi vào đâu đó khác: một lần nạp báo `succeeded`
    mà không có dòng nào trong bronze là chính xác loại hỏng mà cả Task 11 tồn
    tại để chặn — im lặng, và chỉ lộ ra khi có người đi tìm dữ liệu của mình.
    """
    raise SinkNotBuiltYet(
        f"chưa có đường ghi bronze cho lakehouse {spec.lakehouse_id} — "
        "vòng lặp incremental đã xong, `Sink` thật là việc của Task 12"
    )


def _build_connector(spec: IngestSpec) -> Connector:
    credentials = SourceCredentials()
    connector = PostgresConnector(dsn=_source_dsn(spec.source, credentials))
    # `check()` TRƯỚC `discover()`: cả hai đều mở kết nối, nhưng chỉ `check()` trả
    # về một thông báo người vận hành đọc được thay vì một traceback psycopg —
    # xem docstring của nó. Một nguồn không nối được phải thành `error` của run,
    # không thành một stack trace trong log của một pod sắp bị dọn.
    result = connector.check()
    if not result.ok:
        raise SourceUnreachable(result.message)
    return connector


def ingest(client: IngestClientLike, spec: IngestSpec) -> int:
    """Một lần nạp `incremental`, từ spec tới dòng cuối. Trả về số dòng đã đọc."""
    if spec.mode != "incremental":
        raise ModeNotBuiltYet(f"mode {spec.mode!r} chưa được nối ở ảnh này — xem Task 12")

    connector = _build_connector(spec)
    # `discover()` một lần, dùng cho việc chọn cursor (và cho phép đối chiếu
    # schema của Task 14 — cùng một lượt đọc `information_schema`, không phải
    # hai).
    cursor = resolve_cursor(connector.discover(), spec.stream, spec.cursor_column)
    logger.info(
        "ingest.starting",
        stream=spec.stream,
        cursor_column=cursor.name,
        cursor_type=cursor.cursor_type,
        resuming_from=spec.cursor_value,
    )
    return run_incremental(
        connector=connector,
        sink=_build_sink(spec),
        client=client,
        stream=spec.stream,
        cursor=cursor,
    )


def run_reporting_the_outcome(client: IngestClientLike, work: Callable[[], int]) -> int:
    """Chạy `work`, rồi ĐÓNG run theo đúng hướng nó kết thúc.

    Bắt `Exception` rộng CÓ CHỦ ĐÍCH, và hẹp hơn `BaseException` cũng có chủ
    đích: mọi lỗi của một lần nạp (nguồn chết, schema lạ, một `KeyError` không ai
    lường) là thứ control plane cần biết, nên chúng cùng thành một
    `complete(status="failed")`; còn `KeyboardInterrupt`/`SystemExit` không phải
    một lần nạp hỏng — chúng là ai đó đang dừng tiến trình, và biến chúng thành
    `failed` sẽ nói dối về nguyên nhân.

    NÉM LẠI sau khi báo: mã thoát của tiến trình là thứ Kubernetes đọc để đánh
    Job `Failed`, và nuốt ngoại lệ ở đây sẽ cho ra một Job "Complete" chứa một
    run `failed` — hai nguồn sự thật nói ngược nhau.

    Một `complete` hỏng KHÔNG được che mất lỗi gốc: `loom-api` không với tới được
    là lý do rất thường của việc này, và lỗi cần đọc vẫn là lỗi đầu tiên.
    """
    try:
        rows = work()
    except Exception as exc:
        logger.exception("ingest.failed")
        try:
            client.complete(status="failed", error=_describe(exc))
        except Exception:
            # `logger.exception` chứ không nuốt im: nếu cả đường báo cũng hỏng
            # thì log của pod là chỗ DUY NHẤT còn lại để đọc, cho tới khi vòng
            # đối chiếu của Task 13 dọn hàng `running` này.
            logger.exception("ingest.complete_failed_after_failure")
        raise
    client.complete(status="succeeded")
    logger.info("ingest.succeeded", rows=rows)
    return rows


def _describe(exc: BaseException) -> str:
    """Một dòng lý do, KHÔNG BAO GIỜ rỗng.

    `IngestCompletionReport` từ chối một run `failed` không kèm lý do (đúng: một
    run hỏng không nói vì sao thì không dẫn người vận hành đi đâu cả). Nhưng
    `str(exc)` của `raise RuntimeError()` là chuỗi rỗng, nên nếu ghép thẳng, một
    ngoại lệ không có thông báo sẽ làm chính lời báo lỗi bị từ chối — và run kẹt
    ở `running` vì đã hỏng. Tên lớp luôn có, nên nó đứng đầu.
    """
    text = str(exc).strip()
    described = f"{type(exc).__name__}: {text}" if text else type(exc).__name__
    return described[:_ERROR_LIMIT]


def main() -> int:
    settings = Settings()
    client = IngestClient(
        base_url=settings.api_base_url,
        run_id=settings.run_id,
        shared_secret=settings.shared_secret,
    )
    # `client.spec()` nằm TRONG phạm vi báo lỗi: nó là lời gọi chuyển run sang
    # `running` (xem `IngestClient.spec`), nên một lỗi ngay sau nó — kể cả lỗi
    # của chính nó ở lần thử lại — vẫn phải đóng được run.
    run_reporting_the_outcome(client, lambda: ingest(client, client.spec()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
