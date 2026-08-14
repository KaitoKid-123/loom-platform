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

**Đường ghi bronze (Iceberg) được nối Ở ĐÂY từ Task 12** — xem `_build_sink` và
`loom_task.sink`. Cả hai mode đã có đường ghi thật: `incremental` (Task 11) ghi
và commit từng lô vào bảng đích, `full` (Task 12) ghi vào bảng tạm rồi tráo tên
ba bước. Tên bảng đích đến từ `target_table` — một hàm riêng, vì nó là quyết định
không được đổi sau khi có dữ liệu thật và vì vậy phải có phép canh rẻ.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from urllib.parse import quote

import structlog

from loom_connector import Connector
from loom_connector.postgres import PostgresConnector
from loom_core.schemas import IngestSourceSpec, IngestSpec
from loom_iceberg import Lakehouse, build_catalog
from loom_task.client import IngestClient, IngestClientLike
from loom_task.config import LakehouseSettings, ReadTuning, Settings, SourceCredentials
from loom_task.runner import (
    bronze_table_name,
    check_schema,
    resolve_cursor,
    run_full,
    run_incremental,
    source_columns,
)
from loom_task.sink import IcebergSink

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


def target_table(spec: IngestSpec) -> str:
    """Bảng bronze mà run này ghi vào — TÊN, tách khỏi việc mở kết nối.

    Hàm riêng chứ không một dòng trong `_build_sink`, vì đây là quyết định duy
    nhất trong cả đường nạp mà một phép canh KHÔNG THỂ chạm tới nếu nó nằm trong
    đó: `_build_sink` gọi `build_catalog`, và `RestCatalog.__init__` của PyIceberg
    gọi `GET /v1/config` ngay lúc dựng — nên không có test nào pin được tên bảng
    mà không dựng một Lakekeeper. Tên bảng bronze là thứ KHÔNG được đổi sau khi có
    dữ liệu thật (đổi quy ước làm dữ liệu cũ trông như đã biến mất), nên nó phải
    có một phép canh rẻ, và đây là hình dạng làm được điều đó.

    `connection_slug`, KHÔNG `connection_id`: xem `IngestSpec.connection_slug` cho
    lý do hai trường tồn tại cạnh nhau và cái nào đi vào đâu.
    """
    return bronze_table_name(spec.connection_slug, spec.stream)


def _build_sink(spec: IngestSpec) -> IcebergSink:
    """Đường ghi bronze THẬT: một `Lakehouse` trên warehouse của lakehouse này.

    Kiểu trả về là `IcebergSink` chứ không Protocol `Sink`: `ingest` dưới đây còn
    gọi `target_columns()` cho phép đối chiếu schema, và phương thức đó CỐ Ý
    không thuộc `Sink` (xem docstring của nó). `IcebergSink` khớp `Sink` theo cấu
    trúc nên hai vòng lặp nạp vẫn nhận nó y nguyên.

    `warehouse=str(spec.lakehouse_id)` là quy ước đã có từ Giai đoạn 2b
    (`loom_query.runner`, và `loom_api.warehouse_provisioning` là bên tạo):
    warehouse của Lakekeeper mang tên `item.id`, KHÔNG mang `item.name` — tên đổi
    được, id thì không. (Ngược hẳn với TÊN BẢNG bronze, chỗ slug thắng vì tên bảng
    phải đọc được — xem `target_table`. Hai quy ước khác nhau cho hai thứ khác
    nhau, có chủ đích.)
    """
    lakehouse_settings = LakehouseSettings()
    catalog = build_catalog(
        catalog_uri=lakehouse_settings.catalog_uri,
        warehouse=str(spec.lakehouse_id),
        s3_endpoint=lakehouse_settings.s3_endpoint,
    )
    return IcebergSink(
        Lakehouse(catalog),
        target=target_table(spec),
        run_id=spec.run_id,
        stream=spec.stream,
    )


def _build_connector(spec: IngestSpec) -> Connector:
    """Nguồn ĐÃ MỞ ĐƯỢC, với đúng cỡ lô mà cấu hình nói — không phải cỡ lô mặc
    định của connector.

    `batch_rows` phải được truyền TƯỜNG MINH: `PostgresConnector` có mặc định
    riêng là 10 000, nên bỏ tham số này ra không phải là "không cấu hình" mà là
    "cấu hình bằng một con số khác, im lặng". Đúng lỗi đó đã chạy thật trong
    production tới trước commit này, và ĐO 3 định giá nó ở 1,5 MB/s thay vì 3,6
    MB/s — hơn một nửa thông lượng bị mất mà không có dòng log nào nhắc tới. Xem
    `ReadTuning` cho gốc của con số và cho trần RAM chặn nó ở 40 000.
    """
    credentials = SourceCredentials()
    connector = PostgresConnector(
        dsn=_source_dsn(spec.source, credentials),
        batch_rows=ReadTuning().batch_rows,
    )
    # `check()` TRƯỚC `discover()`: cả hai đều mở kết nối, nhưng chỉ `check()` trả
    # về một thông báo người vận hành đọc được thay vì một traceback psycopg —
    # xem docstring của nó. Một nguồn không nối được phải thành `error` của run,
    # không thành một stack trace trong log của một pod sắp bị dọn.
    result = connector.check()
    if not result.ok:
        raise SourceUnreachable(result.message)
    return connector


def ingest(client: IngestClientLike, spec: IngestSpec) -> int:
    """Một lần nạp, từ spec tới dòng cuối. Trả về số dòng đã ĐỌC.

    HAI mode, HAI hàm, không một hàm có cờ: chúng có hai hợp đồng
    đứt-giữa-chừng khác nhau (spec mục 3.1) và chỉ một trong hai có watermark.

    `resolve_cursor` chỉ chạy cho `incremental`, và đó là một tính chất chứ không
    một tối ưu: một bảng không có cột nào dùng được làm cursor vẫn nạp `full`
    được (xem `CursorNotAvailable`), nên hỏi cursor ở đường `full` sẽ TỪ CHỐI
    đúng những bảng mà `full` tồn tại để phục vụ.

    **`check_schema` cũng chỉ chạy cho `incremental`, và đây là một quyết định,
    không phải một chỗ bỏ sót.** Hai lý do, theo thứ tự quan trọng:

    1. `full` KHÔNG nối lô vào bảng cũ: nó ghi vào staging rồi TRÁO tên (xem
       `run_full`), và bảng staging sinh schema từ chính dữ liệu mới. Nên một cột
       thêm/mất ở nguồn không làm `full` hỏng — nó thay cả bảng, đúng như tên gọi.
    2. Một lần nạp `full` CHÍNH LÀ cách sửa tay mà thông báo của `SchemaDrift`
       chỉ tới. Canh cả `full` nghĩa là 3a từ chối luôn con đường thoát duy nhất
       nó có, và người dùng bị kẹt không còn cách nào đi tiếp.

    Cái giá, nói thẳng: một lần `full` sau khi nguồn đổi cột sẽ ĐỔI schema bảng
    bronze mà không hỏi ai. Đó là hành vi của "thay cả bảng", không phải tiến hoá
    schema (không có `ALTER` nào chạy, không có dữ liệu cũ nào được đọc dưới
    schema mới) — nhưng người đọc bảng vẫn thấy một cột mới xuất hiện sau một lần
    nạp, và 3a không có gì cảnh báo họ về điều đó.
    """
    connector = _build_connector(spec)
    if spec.mode == "full":
        logger.info("ingest.starting", stream=spec.stream, mode="full")
        return run_full(
            connector=connector,
            sink=_build_sink(spec),
            client=client,
            stream=spec.stream,
        )

    # `discover()` MỘT lần, dùng cho cả hai việc: đối chiếu schema và chọn cursor
    # — cùng một lượt đọc `information_schema`, không phải hai.
    streams = connector.discover()
    sink = _build_sink(spec)

    # Đối chiếu schema TRƯỚC lô đầu tiên, và trước cả `resolve_cursor`: một cột
    # thêm/mất ở nguồn giải thích được nhiều hơn hẳn một `CursorNotAvailable`
    # (thứ mà chính việc đổi cột có thể gây ra), nên nói ra sự lệch lớn trước là
    # nói ra nguyên nhân thay vì một hệ quả của nó.
    #
    # `None` nghĩa là bảng bronze CHƯA tồn tại — lần nạp đầu tiên của stream này,
    # và không có gì để so: schema đích sẽ được sinh ra TỪ lô đầu (xem
    # `IcebergSink._write`), nên định nghĩa của "lệch" chưa tồn tại ở thời điểm
    # đó. Đây là đường mà MỌI stream đi qua đúng một lần, nên nó phải là một
    # nhánh tường minh chứ không một tập rỗng tình cờ khớp.
    target = sink.target_columns()
    if target is not None:
        check_schema(source=source_columns(streams, spec.stream), target=target)

    cursor = resolve_cursor(streams, spec.stream, spec.cursor_column)
    logger.info(
        "ingest.starting",
        stream=spec.stream,
        mode="incremental",
        cursor_column=cursor.name,
        cursor_type=cursor.cursor_type,
        resuming_from=spec.cursor_value,
    )
    return run_incremental(
        connector=connector,
        sink=sink,
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
