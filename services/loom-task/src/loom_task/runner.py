"""HAI vòng lặp nạp, HAI hợp đồng đứt-giữa-chừng. Xem spec 3a mục 3.1.

`incremental`: GHI TRƯỚC, BÁO SAU — commit từng lô, nạp lại đi tiếp.
`full`: ghi hết vào bảng STAGING (commit từng lô), rồi TRÁO tên ba bước.

`Sink` là một Protocol chứ không phải `loom_iceberg.Lakehouse` trực tiếp: nó cho
phép kiểm THỨ TỰ mà không cần Iceberg, và chính thứ tự mới là chỗ mất dữ liệu
sinh ra — ở `incremental` là mất dòng im lặng, ở `full` là mất cả bảng cũ. Đường
ghi Iceberg thật (`loom_task.sink.IcebergSink`) được kiểm ở integration.

**`full` KHÔNG nguyên tử, và không có chỗ nào trong file này được nói ngược lại.**
Thiết kế "một commit ở cuối" đã bị ĐO 2 bác bỏ bằng số: `table.transaction()` của
PyIceberg 0.11.1 không gộp (hai `append` cho 2 snapshot), và gom trong transaction
còn tốn 478 MiB so với 421 MiB khi commit từng lô. Cú tráo tên vì vậy phải qua
nhiều lời gọi catalog, và giữa chúng có một cửa sổ mà tên bảng đích không phân
giải được. `full` là GẦN nguyên tử — xem `run_full`.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Protocol

import pyarrow as pa  # type: ignore[import-untyped]

from loom_connector import Connector, CursorCandidate, StreamSchema, StreamState
from loom_core.cursor import format_cursor_value
from loom_task.client import IngestClientLike

# Ba cột metadata của bronze (spec v1 mục 5.5). `check_schema` loại trừ đúng ba
# tên này, nếu không MỌI lần nạp thứ hai đều báo schema drift.
BRONZE_COLUMNS = ("_ingested_at", "_source", "_batch_id")


class Boom(RuntimeError):
    """Chỉ dùng để kiểm nạp lại. KHÔNG bắt được ở mã sản xuất, và không có đường
    nào ném nó ngoài `crash_after_batch` — một tham số mà `main.py` không truyền."""


class StreamNotDiscovered(RuntimeError):
    """Nguồn không có stream mà run này nói tên.

    Tách khỏi `CursorNotAvailable` vì hai lỗi này sửa ở hai chỗ khác nhau: tên
    stream sai là sửa ở cấu hình run (hoặc bảng đã bị xoá ở nguồn), còn thiếu
    cursor dùng được là chọn nhầm mode — `full` chạy được trên một bảng không có
    cột nào không-giảm-dần, `incremental` thì không.
    """


class CursorNotAvailable(RuntimeError):
    """Không có cột nào của stream này dùng được làm watermark, hoặc cột mà
    watermark đang đứng trên không còn dùng được ở nguồn."""


class SchemaDrift(RuntimeError):
    """Nguồn và bảng bronze không khớp. 3a KHÔNG tiến hoá schema — spec mục 7."""


class Sink(Protocol):
    """Bề mặt ghi mà HAI vòng lặp cần, và tên phương thức = tên sự kiện.

    **Một Protocol cho cả hai mode, không hai.** `main._build_sink` dựng ĐÚNG
    MỘT đối tượng cho một lần chạy (nó chưa biết mode khi dựng), nên tách thành
    `Sink` + `FullSink` chỉ thêm một cái tên thứ hai cho cùng một lớp mà không
    thêm phép canh nào. Đổi lại, `run_incremental` nhận một tham số khai 7
    phương thức trong khi nó gọi 1 — nói ra để không ai đọc `Sink` thành "mọi
    phương thức ở đây đều dùng ở mọi đường".

    **Tên phương thức TRÙNG tên sự kiện mà `RecordingSink` ghi lại** (`stage`,
    `staging_done`, `rename_target_away`, `promote_staging`, `drop_old_target`).
    Một bản nháp trước dùng `stage`/`commit_replacing` cho protocol nhưng
    `staging_done`/`promote_staging` cho test — hai bộ từ vựng cho một hợp đồng,
    nên không đọc được test nào canh phương thức nào. Ngoại lệ duy nhất là
    `append` (ghi sự kiện `"write"`, có từ Task 11): đổi nó bây giờ chỉ để cho
    đều là sửa ba bài test đang xanh mà không mua thêm gì.
    """

    def append(self, batch: pa.RecordBatch) -> None:
        """Ghi VÀ commit ngay VÀO BẢNG ĐÍCH — hợp đồng của `incremental`.

        Một `append` chỉ ghi file dữ liệu mà chưa commit sẽ phá đúng tính chất
        mà `run_incremental` dựa vào: sau khi nó trả về, dữ liệu của lô này phải
        ĐÃ bền, vì watermark được báo ngay sau đó.
        """

    def stage(self, batch: pa.RecordBatch) -> None:
        """Ghi VÀ commit một lô vào bảng STAGING — không chạm bảng đích.

        Commit TỪNG LÔ có chủ đích, và đây là chỗ dễ đọc ngược nhất trong cả
        Task 12: ĐO 2 đã bác bỏ "gom rồi commit một lần" bằng số đo (không gộp
        được snapshot, và tốn thêm 57 MiB RAM), nên commit từng lô là hành vi
        ĐÚNG ở đây, không phải một thoả hiệp. Cái làm nó an toàn không phải số
        lần commit mà là ĐÍCH của chúng: staging là một bảng khác, nên một lần
        chạy đứt giữa chừng không để lại lô nửa vời trong bảng người dùng đang
        đọc.
        """

    def staging_done(self) -> None:
        """Chốt staging: sau lời gọi này bảng staging phải TỒN TẠI và đủ dòng.

        Có mặt vì `run_full` cần một điểm phân chia QUAN SÁT ĐƯỢC giữa "đang
        ghi" và "đang tráo" — `test_full_writes_everything_to_staging_before_
        touching_the_target` cắt chuỗi sự kiện ở đúng đây. Và nó có việc thật:
        một lần chạy không ghi được lô nào (nguồn rỗng) thì bảng staging chưa
        bao giờ được tạo, và phải hỏng ồn ào ở đây thay vì hỏng ở
        `promote_staging` dưới dạng một `NoSuchTableError` không nhắc gì tới
        nguyên nhân.
        """

    def target_exists(self) -> bool:
        """Bảng đích đã tồn tại chưa — quyết định lần nạp này đi đường nào.

        KHÔNG ghi sự kiện: đây là một câu HỎI, và lần nạp `full` ĐẦU TIÊN của
        một lakehouse (chưa có bảng đích) phải cho ra một chuỗi sự kiện KHÔNG
        có `rename_target_away` nào — xem `run_full`.
        """

    def rename_target_away(self) -> None:
        """Bước 2: `rename(đích -> đích_cũ)`. Dữ liệu cũ SỐNG dưới tên khác."""

    def promote_staging(self) -> None:
        """Bước 3: `rename(staging -> đích)`. Cửa sổ đóng lại ở đây."""

    def drop_old_target(self) -> None:
        """Bước 4: bỏ `đích_cũ` khỏi catalog. KHÔNG xoá object dưới S3.

        Giai đoạn 2c đã chứng minh `drop_table` không chạm data file trên S3
        (xem `Lakehouse.drop_table`), nên bước này giải phóng cái TÊN chứ không
        giải phóng đĩa. Dọn đĩa là nợ có tên ở spec mục 13.
        """


def bronze_table_name(connection_slug: str, stream: str) -> str:
    """`bronze.<slug connection>__<schema nguồn>_<bảng nguồn>`.

    HAI dấu gạch dưới ngăn phần connection với phần bảng, để hai nguồn khác nhau
    có bảng trùng tên không đụng nhau, và để đọc ngược ra được nguồn từ tên bảng.
    Một dấu gạch thôi thì `pos_public_orders` không phân biệt được với connection
    tên `pos_public` đọc bảng `orders`.

    **Dấu `-` của slug thành `_`, và đó là một phép sửa lỗi, không phải trang
    trí.** `IngestSpec.connection_slug` là `item.name`, mà `ItemCreate.name` canh
    `^[a-z0-9][a-z0-9-]*$` — tức là tên connection THƯỜNG có gạch ngang
    (`can-sua`, `pos-aiven`), đó là khuôn bình thường của repo này chứ không phải
    một trường hợp biên. Và một gạch ngang trong tên bảng làm bảng đó KHÔNG TRUY
    VẤN ĐƯỢC nếu không trích dẫn: đã dựng lại thật trên DuckDB (engine mà
    `loom-query` chạy) — `SELECT * FROM bronze.pos-aiven__public_orders` trả
    `ParserException: syntax error at or near "-"`, còn cùng câu đó với
    `"pos-aiven__public_orders"` trong ngoặc kép thì chạy. Ví dụ của spec mục 5
    cũng là `pos_aiven`, gạch DƯỚI.

    Phép đổi này KHÔNG làm hai connection khác nhau đụng vào một bảng: `item.name`
    không được phép chứa `_` (xem pattern ở trên), nên trên bộ ký tự đó phép
    `-` -> `_` là ĐƠN ÁNH — hai tên khác nhau lệch ở ít nhất một vị trí, và sau
    phép đổi vẫn lệch ở đúng vị trí ấy.

    Chuyện KHÔNG được bảo đảm, nói ra vì nó nhìn giống chuyện trên: một tên bị
    NHẢ RA rồi dùng lại (`uq_item_active_name` chỉ duy nhất trong phạm vi item còn
    `active`, nên xoá mềm một connection là nhả tên nó) trỏ hai `connection_id`
    khác nhau vào CÙNG một bảng bronze qua thời gian. Đó là lý do cột `_source`
    mang `connection_id` chứ không mang slug — xem `IngestSpec`.

    Một slug sinh ra `__` sau phép đổi (tên `pos--aiven`, hợp lệ theo pattern) bị
    TỪ CHỐI: nó dựng một dấu ngăn thứ hai và làm chính câu "đọc ngược ra được
    nguồn từ tên bảng" ở trên thành sai. Từ chối ồn ào chứ không thu gọn `--` về
    một `_`: thu gọn thì `pos-aiven` và `pos--aiven` cho ra CÙNG một bảng, tức là
    hai nguồn ghi đè lẫn nhau trong im lặng — đổi một lỗi đọc-được lấy một lỗi
    mất dữ liệu.
    """
    schema, _, table = stream.partition(".")
    if not schema or not table:
        raise ValueError(f"stream phải là 'schema.table', nhận {stream!r}")
    encoded = connection_slug.replace("-", "_")
    if "__" in encoded:
        raise ValueError(
            f"tên connection {connection_slug!r} có hai dấu ngăn liền nhau sau khi "
            f"đổi '-' thành '_' ({encoded!r}) — nó sẽ trùng khuôn với dấu ngăn "
            "'__' giữa phần connection và phần bảng, và tên bảng bronze không còn "
            "đọc ngược ra được nguồn. Đổi tên connection (một dấu gạch ngang một "
            "lần) rồi nạp lại"
        )
    return f"bronze.{encoded}__{schema}_{table}"


def add_bronze_columns(batch: pa.RecordBatch, source: str, batch_id: uuid.UUID) -> pa.RecordBatch:
    """Thêm đúng ba cột `BRONZE_COLUMNS` vào cuối lô.

    `_ingested_at` lấy đồng hồ của tiến trình này MỘT lần cho cả lô: một dấu thời
    gian cho mỗi DÒNG sẽ nói rằng các dòng trong cùng một lô hạ cánh ở những thời
    điểm khác nhau, điều không đúng — chúng vào bằng một commit.
    """
    rows = batch.num_rows
    now = datetime.now(UTC)
    return pa.RecordBatch.from_arrays(
        [
            *batch.columns,
            pa.array([now] * rows, type=pa.timestamp("us", tz="UTC")),
            pa.array([source] * rows, type=pa.string()),
            pa.array([str(batch_id)] * rows, type=pa.string()),
        ],
        names=[*batch.schema.names, *BRONZE_COLUMNS],
    )


def _schema_for(streams: Sequence[StreamSchema], stream: str) -> StreamSchema:
    """Stream mà run này nói tên, hoặc `StreamNotDiscovered`.

    Dùng CHUNG bởi `source_columns` và `resolve_cursor`: hai câu hỏi khác nhau
    trên cùng MỘT lượt `discover()`, nhưng cùng một câu trả lời cho "nguồn không
    có bảng đó". Viết phép tra hai lần là mở đường cho hai thông báo lệch nhau
    mô tả cùng một sự cố.
    """
    schema = next((s for s in streams if s.name == stream), None)
    if schema is None:
        raise StreamNotDiscovered(
            f"nguồn không có stream {stream!r} — discover() thấy {sorted(s.name for s in streams)}"
        )
    return schema


def source_columns(streams: Sequence[StreamSchema], stream: str) -> list[str]:
    """Tên MỌI cột của stream ở nguồn — đầu vào `source` của `check_schema`.

    MỌI cột, không chỉ `candidate_cursors`: drift là chuyện của cả bảng, còn
    `candidate_cursors` chỉ là những cột dùng được làm watermark (một tập con
    lọc theo kiểu, xem `CursorCandidate`). Lấy nhầm tập con đó thì một cột `text`
    mới thêm ở nguồn không bao giờ bị coi là drift.
    """
    return [column.name for column in _schema_for(streams, stream).columns]


def check_schema(source: list[str], target: list[str]) -> None:
    """So TÊN cột theo TẬP HỢP, và ném `SchemaDrift` khi hai bên lệch.

    **Chỉ so TÊN, KHÔNG so KIỂU — đây là giới hạn thật của phép canh này, không
    phải một chi tiết cài đặt.** Một cột `id` đổi từ `integer` sang `text` ở
    nguồn đi qua đây mà không một lời nào. Nó không lặng lẽ làm hỏng dữ liệu
    (lần `append` vào Iceberg sẽ hỏng vì lệch schema), nhưng nó hỏng ở một chỗ
    XA nguyên nhân và với một thông báo không nhắc gì tới việc nguồn đã đổi
    kiểu. Nói ra ở đây vì một phép canh tự nhận là canh "schema" trong khi chỉ
    canh tên cột là đúng loại nhầm lẫn đắt tiền.

    **Theo TẬP HỢP, không theo thứ tự.** Thứ tự cột ở nguồn đổi được mà không
    đổi ý nghĩa gì (thêm một cột ở giữa, `SELECT *` trả thứ tự khác sau một
    `ALTER`), nên so theo thứ tự sẽ báo drift cho một thay đổi vô hại — và một
    phép canh báo động giả là một phép canh sắp bị tắt đi.

    **Loại trừ ba cột `BRONZE_COLUMNS` khỏi phía đích.** CHÚNG TA thêm chúng
    (`add_bronze_columns`), nguồn không bao giờ có. Không loại trừ thì MỌI lần
    nạp thứ hai đều báo drift.

    Trừ ở phía ĐÍCH chứ không thêm vào phía nguồn: nếu một ngày nguồn thật sự có
    một cột tên `_source`, phép trừ này cho ra "nguồn có thêm: _source" — một
    thông báo đúng và đọc được — thay vì âm thầm coi hai cột khác nhau là một.
    """
    target_real = set(target) - set(BRONZE_COLUMNS)
    source_set = set(source)
    if target_real == source_set:
        return

    added = sorted(source_set - target_real)
    removed = sorted(target_real - source_set)
    parts = []
    if added:
        parts.append(f"nguồn có thêm: {', '.join(added)}")
    if removed:
        parts.append(f"nguồn không còn: {', '.join(removed)}")
    raise SchemaDrift(
        "schema nguồn khác bảng bronze — " + "; ".join(parts) + ". "
        "Giai đoạn 3a không tiến hoá schema; sửa tay hoặc chờ 3b."
    )


def resolve_cursor(
    streams: Sequence[StreamSchema], stream: str, remembered_column: str | None
) -> CursorCandidate:
    """Cột watermark và KIỂU của nó, lấy từ CONNECTOR — không đoán ở đâu khác.

    **Vì sao hàm này tồn tại.** `IngestSpec.cursor_type` là `None` cho tới khi có
    watermark, nên ở lần nạp `incremental` ĐẦU TIÊN của một stream, control plane
    không có kiểu để đưa. `IngestProgressReport` thì đòi `cursor_type` ngay từ lô
    đầu. Chỗ duy nhất biết kiểu THẬT của cột ở nguồn là connector — nó đọc
    `information_schema.columns.data_type` để lọc `candidate_cursors` — nên kiểu
    đi ra từ `discover()` (xem `CursorCandidate`), không phải từ một bảng ánh xạ
    ngược `arrow_type -> kiểu Postgres` dựng thêm ở đây.

    **`remembered_column` (tức `IngestSpec.cursor_column`) THẮNG khi có.** Đổi
    cột giữa hai lần chạy làm `loom-api` ĐẶT LẠI watermark thay vì so sánh (hai
    thang đo khác nhau — xem `_advance_watermark`), tức là đọc lại từ đầu. Nên
    một khi đã chọn, lựa chọn đó phải giữ nguyên, và nơi nó được nhớ là hàng
    `stream_state`.

    **Lần đầu thì lấy ứng viên ĐẦU TIÊN, và đó là một lựa chọn của mã này, không
    phải của người dùng.** Giai đoạn 3a chưa có ô nào để chọn cột cursor (3c mới
    có UI), nên phải có ai đó chọn; thứ tự của `candidate_cursors` là
    `ordinal_position` ở nguồn. Nói ra để không ai đọc nó thành "cột tốt nhất".
    Điều làm lựa chọn này an toàn được là nó chỉ xảy ra MỘT lần cho mỗi stream:
    lô đầu báo cột đó về, và từ đó `remembered_column` quyết định.

    Cột đã nhớ mà không còn trong `candidate_cursors` thì NÉM, không rơi về ứng
    viên khác: kiểu ở nguồn vừa đổi (một `ALTER TABLE ... TYPE text`) nghĩa là
    watermark cũ có thể không còn đọc được dưới kiểu mới, và tự chọn một cột khác
    sẽ lặng lẽ nạp lại toàn bộ bảng dưới cái tên "incremental".
    """
    schema = _schema_for(streams, stream)
    if not schema.candidate_cursors:
        raise CursorNotAvailable(
            f"stream {stream!r} không có cột nào dùng được làm watermark "
            "(kiểu phải nằm trong CURSOR_TYPE_ALLOWLIST) — mode 'incremental' "
            "không chạy được trên nó, 'full' thì được"
        )
    if remembered_column is None:
        return schema.candidate_cursors[0]

    remembered = next((c for c in schema.candidate_cursors if c.name == remembered_column), None)
    if remembered is None:
        raise CursorNotAvailable(
            f"watermark của stream {stream!r} đứng trên cột {remembered_column!r}, "
            "nhưng nguồn không còn coi cột đó là cursor dùng được (kiểu đã đổi, "
            "hoặc cột đã bị xoá) — sửa ở nguồn, hoặc nạp lại bằng mode 'full'"
        )
    return remembered


def _max_cursor(batch: pa.RecordBatch, cursor_column: str, cursor_type: str) -> str:
    """Mốc cao nhất của lô, ĐÃ tuần tự hoá bằng chính hàm mà server dùng để đọc.

    KHÔNG phải `str(max(...))`: giá trị này đi qua JSON tới
    `loom_core.cursor.parse_cursor_value`, nên định dạng là một HỢP ĐỒNG
    liên-service. `str(int)` thì an toàn, nhưng `str(datetime)` cho một dấu CÁCH
    thay vì `T` và chỉ chạy được vì `fromisoformat` của Python 3.11+ tình cờ nhận
    dấu cách — may, không phải thiết kế. `format_cursor_value` là phép nghịch
    tường minh của `parse_cursor_value`, và test khứ hồi qua cả sáu kiểu
    (`packages/core/tests/test_cursor.py`) là thứ bắt được một bên đổi định dạng
    mà bên kia không biết.

    `max()` chứ không "dòng cuối": nó đúng bất kể connector có ORDER BY hay
    không, và một watermark lấy từ dòng cuối của một lô KHÔNG sắp xếp sẽ tiến qua
    những giá trị lớn hơn nằm ở giữa lô — mất dòng, im lặng.
    """
    return format_cursor_value(cursor_type, max(batch.column(cursor_column).to_pylist()))


def run_incremental(
    connector: Connector,
    sink: Sink,
    client: IngestClientLike,
    stream: str,
    cursor: CursorCandidate,
    crash_after_batch: int | None = None,
) -> int:
    """GHI TRƯỚC, BÁO SAU. Đảo lại là mất dòng — xem test cùng tên.

    Nếu watermark được báo TRƯỚC khi lô được commit, một pod chết giữa hai bước
    làm watermark tiến qua dữ liệu chưa hề được ghi, và lần nạp sau bỏ qua đúng
    khoảng đó. Không có gì phát hiện được điều đó về sau — bảng chỉ đơn giản là
    thiếu dòng, không lỗi, không dấu vết. Thứ tự này cho TRÙNG chứ không cho
    MẤT, khớp hợp đồng at-least-once của spec mục 4.

    `crash_after_batch` chỉ để test nạp lại (`main.py` không truyền nó): nó ném
    `Boom` SAU khi lô thứ N đã ghi và đã báo, mô phỏng đúng chỗ đứt mà một
    OOMKill tạo ra.
    """
    total = 0
    for index, batch in enumerate(connector.read(stream, client.current_state())):
        enriched = add_bronze_columns(batch, client.source_id, uuid.uuid4())
        # 1. ghi VÀ commit lô này
        sink.append(enriched)
        # 2. chỉ khi (1) đã xong mới báo watermark
        client.report_progress(
            cursor_column=cursor.name,
            cursor_type=cursor.cursor_type,
            cursor_value=_max_cursor(batch, cursor.name, cursor.cursor_type),
            rows=batch.num_rows,
        )
        total += batch.num_rows
        if crash_after_batch is not None and index + 1 >= crash_after_batch:
            raise Boom("crash có chủ đích để kiểm nạp lại")
    return total


def run_full(
    connector: Connector,
    sink: Sink,
    client: IngestClientLike,
    stream: str,
    crash_after_batch: int | None = None,
) -> int:
    """Đọc CẢ bảng vào staging, rồi TRÁO tên BA bước. Trả về số dòng đã đọc.

    ```
    1. ghi hết vào staging, commit TỪNG LÔ   <- RAM có chặn; đích còn nguyên
    2. rename(đích -> đích_cũ)
    3. rename(staging -> đích)               <- cửa sổ nằm giữa 2 và 3
    4. drop(đích_cũ)
    ```

    **Vì sao BA bước chứ không `drop(đích)` rồi `rename(staging -> đích)`.** Hai
    bước trông gọn hơn và là cái bẫy: nó kéo cửa sổ ra suốt thao tác `drop`, và
    nếu `rename` hỏng ngay sau đó thì dữ liệu cũ ĐÃ MẤT còn dữ liệu mới CHƯA
    VÀO — mất trắng, không lùi lại được. Ba bước giữ dữ liệu cũ sống dưới một
    tên khác tới bước cuối, nên hỏng ở giữa còn đổi tên ngược lại được bằng tay.
    `test_the_swap_renames_the_target_away_before_promoting_staging` canh đúng
    thứ tự này.

    **`full` là GẦN nguyên tử, không nguyên tử** — và không có câu nào ở đây
    được hứa hơn thế. ĐO 2 mục D4 đã đo: `rename_table` TỪ CHỐI đè lên một tên
    đang tồn tại, nên cú tráo không viết được thành một lời gọi duy nhất. Giữa
    bước 2 và bước 3 có một khoảnh khắc tên bảng đích không phân giải được. Cái
    `full` bảo đảm là KHÔNG BAO GIỜ thấy dữ liệu nửa vời; cái nó không bảo đảm
    là bảng luôn tồn tại.

    **Lần nạp `full` ĐẦU TIÊN của một lakehouse không có bảng đích**, và bước 2
    trên một bảng không tồn tại là `NoSuchTableError`. Đường đó là `staging_done`
    rồi `promote_staging` THẲNG — không bước 2, không bước 4. Đây là đường mà
    MỌI lakehouse mới đi qua đúng một lần, nên nó có test riêng
    (`test_the_very_first_full_load_has_no_target_to_rename_away`); hỏng ở đây là
    hỏng ngay lần dùng đầu, ở đúng chỗ không ai kịp có dữ liệu để mất.

    `target_exists()` rồi `rename_target_away()` là hai lời gọi tách rời, nên có
    một khe hở lý thuyết: ai đó tạo bảng đích ngay giữa hai lời gọi. Hậu quả là
    `promote_staging` ném `TableAlreadyExistsError` — hỏng ồn ào, dữ liệu của
    người đó còn nguyên. Không đáng thêm một cơ chế khoá cho một khe hở mà kết
    quả xấu nhất là một run `failed` đọc được.

    **BÁO SỐ DÒNG, KHÔNG BAO GIỜ BÁO CURSOR** — và hai nửa câu đó là hai tính
    chất riêng, không phải một.

    Số dòng thì báo, vì `ingest_run.rows_written` chỉ cộng dồn qua `/progress`
    (`/complete` cố ý không mang `rows` — xem `IngestCompletionReport`), nên một
    `run_full` im lặng để cột đó ở 0 suốt cả lần nạp và người dùng không có con số
    nào để xem. Cursor thì KHÔNG, vì `full` đọc lại từ đầu: không có mốc nào để
    tiến, và một watermark đẩy lên ở chế độ này làm lần `incremental` KẾ TIẾP bỏ
    qua đúng khoảng dữ liệu vừa đọc — mất dòng, im lặng, ở một lần chạy KHÁC.
    `IngestProgressReport` cho phép đúng hình dạng này (cả ba trường cursor
    `None`); `test_full_reports_rows_but_never_a_cursor` canh nó.

    Số dòng báo về đếm những dòng đã vào STAGING, nên một lần chạy đứt giữa chừng
    để lại `rows_written` lớn hơn số dòng thật sự có trong bảng đích (staging bị
    bỏ, bảng đích không đổi). Đó là hình dạng đúng của một chỉ số TIẾN ĐỘ; biến nó
    thành một con số chỉ đúng lúc kết thúc đòi báo tất cả sau cú tráo, tức là
    không có tiến độ nào trong lúc chạy.

    **KHÔNG đụng watermark, và KHÔNG đọc nó.** `connector.read` nhận
    `StreamState()` RỖNG chứ không `client.current_state()`: `full` nghĩa là đọc
    lại từ đầu (spec mục 5), và nếu một watermark lọt vào đây thì lần nạp này
    lặng lẽ trở thành một lần `incremental` mang tên `full` — rồi cú tráo THAY
    cả bảng bằng đúng phần đuôi vừa đọc. Đó là mất dữ liệu, không phải một tối
    ưu. `loom-api` cũng đã không gửi watermark cho mode này (xem `ingest_spec`),
    nên đây là lớp canh thứ hai cho cùng một lỗi.

    `crash_after_batch` chỉ để test (main.py không truyền): nó ném `Boom` SAU khi
    lô thứ N vào staging, đúng chỗ đứt mà một OOMKill tạo ra — và bài test đòi
    bảng đích lúc đó chưa bị chạm tới.
    """
    total = 0
    for index, batch in enumerate(connector.read(stream, StreamState())):
        enriched = add_bronze_columns(batch, client.source_id, uuid.uuid4())
        # 1. ghi VÀ commit lô này vào staging
        sink.stage(enriched)
        # 2. chỉ khi (1) đã xong mới báo — cùng thứ tự với `run_incremental`, dù ở
        #    đây nó không mua được tính đúng đắn nào (không có watermark để tiến
        #    quá dữ liệu chưa ghi). Giữ một thứ tự cho cả hai đường để không ai
        #    phải đọc hai vòng lặp mới biết cái nào báo trước.
        client.report_progress(rows=batch.num_rows)
        total += batch.num_rows
        if crash_after_batch is not None and index + 1 >= crash_after_batch:
            raise Boom("crash có chủ đích để kiểm bảng đích còn nguyên")

    sink.staging_done()
    had_target = sink.target_exists()
    if had_target:
        sink.rename_target_away()
    sink.promote_staging()
    if had_target:
        sink.drop_old_target()
    return total
