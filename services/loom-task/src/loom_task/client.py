"""Nói chuyện với `/internal/ingest/*`. Đây là ĐƯỜNG DUY NHẤT tới control plane.

Task pod KHÔNG có credential Postgres của control plane — nó không bao giờ chạm
database đó trực tiếp, và đó là tính chất THẬT của thiết kế này (spec 3a mục 6).

**Điều KHÔNG đúng, đừng viết vào đây: "một pod bị chiếm chỉ với tới được run của
chính nó".** Câu đó có trong bản nháp kế hoạch (và trong mục 6 của chính spec),
và nó SAI: mọi Job nạp nhận CÙNG MỘT giá trị bí mật qua cùng một `secretKeyRef`
(xem `JobLauncher.launch` — địa chỉ Secret tới từ `Settings.
task_shared_secret_name`/`_key`, hằng số cho cả cụm, không sinh theo từng run),
nên bí mật chỉ chứng minh "MỘT pod nạp đã gửi request này", không chứng minh
"pod nạp CỦA RUN NÀY". Một pod bị chiếm gọi được
`/internal/ingest/<run_id bất kỳ>/progress` nếu nó biết id đó, và `run_id` trong
env chỉ là mặc định pod tự dùng, không phải hàng rào. `loom_api.
internal_security` ghi CÙNG giới hạn đó ở đầu KIỂM, kèm nợ có tên (token theo
từng run, hoãn có chủ ý). Hai đầu phải nói cùng một câu, nếu không thì đầu này
dựng một lời hứa bảo mật mà đầu kia không giữ.

**Model dùng CHUNG với control plane, không phải `dict` tự lắp.** `IngestSpec`/
`IngestProgressReport`/`IngestCompletionReport` sống ở `loom_core.schemas`, và
service này validate bằng chính chúng: một trường bị đổi tên ở `loom-api` thành
một `ValidationError` ồn ào tại pod, thay vì một `.get("cursor_column")` lặng lẽ
ra `None` — tức là lặng lẽ nạp lại từ đầu, hoặc lặng lẽ bỏ watermark. Cùng lý do
`report_progress` DỰNG model rồi mới gửi: hai phép kiểm ở biên của
`IngestProgressReport` (ba trường cursor đi cùng nhau, và giá trị phải đọc được
dưới kiểu nó khai) chạy ở BÊN GỬI, nơi biết mình vừa đọc cột nào — không phải
sau một chặng mạng dưới dạng 422.
"""

from __future__ import annotations

import uuid
from typing import Literal, Protocol

import httpx

from loom_connector import StreamState
from loom_core.internal_auth import INGEST_SHARED_SECRET_HEADER
from loom_core.schemas import IngestCompletionReport, IngestProgressReport, IngestSpec

# Hai hướng kết thúc, và mypy kiểm CHÍNH chúng ở mọi chỗ gọi `complete` —
# `Literal` chứ không `str`, vì một `status="suceeded"` gõ sai chỉ lộ ra dưới
# dạng 422 từ `loom-api` (rồi một run kẹt ở `running`) nếu kiểu ở đây là `str`.
# Cùng `Literal` mà `IngestCompletionReport.status` khai, nên không có phép thu
# hẹp nào phải viết ở giữa.
CompletionStatus = Literal["succeeded", "failed"]

# Đủ để một pod chậm không tự cắt mình giữa lúc control plane còn đang trả lời,
# và ngắn hơn hẳn một lần nạp (phút tới chục phút) nên một `loom-api` treo không
# giữ pod sống mãi. `httpx` KHÔNG có timeout mặc định cho toàn bộ request nếu
# không truyền tham số này.
_TIMEOUT_SECONDS = 30.0


class IngestClientLike(Protocol):
    """Đúng phần bề mặt của `IngestClient` mà `runner.py` gọi tới.

    Ở CẠNH lớp nó mô tả, không ở module người gọi — cùng lập luận với
    `loom_api.jobs.JobLauncherLike`: một Protocol đặt xa lớp thật là hai khai
    báo phải giữ khớp nhau bằng trí nhớ. Nó tồn tại để `tests/doubles.py` thay
    được `IngestClient` mà `runner.py` không phải biết `httpx`.

    Nói cho đúng về phần mypy kiểm được: nó kiểm bản THẬT khớp Protocol này (ở
    chỗ gán trong `main.py`, một module nằm trong `files` của mypy). Test double
    thì KHÔNG được kiểm tĩnh — `pyproject.toml` gốc chỉ cho mypy đọc `src`, không
    đọc `tests` ở bất kỳ package nào — nên một double lệch hình dạng lộ ra khi
    test CHẠY (`TypeError`/`AttributeError`), không lộ ra ở `make lint`.

    **Mọi tham số của `report_progress` là KEYWORD-ONLY, và đó là một ràng buộc
    đúng-sai chứ không phải phong cách.** `cursor_type` và `cursor_value` đều là
    `str`; hoán vị hai đối số theo vị trí không sai kiểu, không sai số lượng, và
    hậu quả là `cursor_type="2026-08-13"` — một `CursorTypeNotAllowed` ở biên nếu
    may, một watermark vô nghĩa nếu không. `*` làm lớp lỗi đó không viết ra được.
    """

    @property
    def source_id(self) -> str: ...

    def current_state(self) -> StreamState: ...

    def report_progress(
        self, *, cursor_column: str, cursor_type: str, cursor_value: str, rows: int
    ) -> None: ...

    def complete(self, *, status: CompletionStatus, error: str | None = None) -> None: ...


class IngestClient:
    def __init__(self, base_url: str, run_id: uuid.UUID, shared_secret: str) -> None:
        self._client = httpx.Client(
            base_url=base_url,
            headers={INGEST_SHARED_SECRET_HEADER: shared_secret},
            timeout=_TIMEOUT_SECONDS,
        )
        self._run_id = run_id
        self._spec: IngestSpec | None = None

    @property
    def source_id(self) -> str:
        """Giá trị của cột bronze `_source` — `connection_id`, không phải host.

        Spec mục 5.5 chọn `connection_id` chứ không phải một chuỗi mô tả nguồn
        (host/database) vì host đổi được (một lần chuyển máy chủ) trong khi id
        thì không, và cột này là thứ silver dùng để nói "dòng này tới từ nguồn
        nào" qua nhiều tháng dữ liệu.
        """
        return str(self.spec().connection_id)

    def spec(self) -> IngestSpec:
        """Lấy spec MỘT lần rồi giữ lại: lời gọi này có TÁC DỤNG PHỤ.

        `GET .../spec` chuyển run từ `pending` sang `running` ở phía control
        plane (xem docstring `ingest_spec` bên `loom-api` cho lý do một GET được
        phép làm thế). Gọi lại không hồi sinh một run đã đóng, nên lặp lại là vô
        hại — nhưng nó vẫn là một round trip cho một giá trị không đổi trong
        suốt một lần chạy, và `source_id` được đọc MỖI LÔ.
        """
        if self._spec is None:
            response = self._client.get(f"/internal/ingest/{self._run_id}/spec")
            response.raise_for_status()
            self._spec = IngestSpec.model_validate(response.json())
        return self._spec

    def current_state(self) -> StreamState:
        """Watermark đang lưu, dịch sang thứ mà `Connector.read` nhận.

        `cursor_type` KHÔNG đi vào `StreamState`: connector lọc nguồn bằng
        `WHERE <cột> >= %s` và để Postgres tự ép kiểu chuỗi đó về kiểu của cột
        (xem `PostgresConnector._read_rows`) — kiểu chỉ cần thiết cho phép SO
        SÁNH watermark, việc của `loom-api`. Đưa nó vào `StreamState` sẽ là một
        trường thứ ba không ai đọc, và một trường không ai đọc là một trường sẽ
        trôi.
        """
        spec = self.spec()
        return StreamState(cursor_column=spec.cursor_column, cursor_value=spec.cursor_value)

    def report_progress(
        self, *, cursor_column: str, cursor_type: str, cursor_value: str, rows: int
    ) -> None:
        """Một lô ĐÃ hạ cánh. Gọi SAU khi ghi, không bao giờ trước — xem `runner`.

        `cursor_type` BẮT BUỘC (`IngestProgressReport` đòi cả ba trường cursor
        cùng lúc): không có nó, phía server chỉ so được CHUỖI, và so chuỗi trên
        cursor nguyên làm watermark kẹt vĩnh viễn ở lần đầu vượt mốc đổi số chữ
        số — `"1000" > "400"` là `False`. Xem `loom_core.cursor`.
        """
        report = IngestProgressReport(
            rows=rows,
            cursor_column=cursor_column,
            cursor_type=cursor_type,
            cursor_value=cursor_value,
        )
        response = self._client.post(
            f"/internal/ingest/{self._run_id}/progress",
            json=report.model_dump(mode="json"),
        )
        response.raise_for_status()

    def complete(self, *, status: CompletionStatus, error: str | None = None) -> None:
        """MỘT route cho cả hai hướng kết thúc — KHÔNG có `/fail`.

        Task 10 dựng đúng BA route (`spec`, `progress`, `complete`), nên một
        `IngestClient.fail()` gọi `/internal/ingest/{run_id}/fail` — như bản nháp
        kế hoạch viết — sẽ là một 404 ở đúng lúc pod đang cố nói rằng nó hỏng.

        `error` bắt buộc khi `status="failed"` và BỊ CẤM khi `"succeeded"`; cả
        hai luật kiểm trong `IngestCompletionReport`, nên dựng model ở đây làm
        một hình dạng sai nổ TẠI ĐÂY thay vì thành 422 sau một chặng mạng — và
        `ingest_run` không bao giờ có một hàng tự mâu thuẫn (thành công kèm lý do
        hỏng, hoặc hỏng mà không nói vì sao).

        `rows` KHÔNG có trong thân request, dù bản nháp gửi nó:
        `IngestCompletionReport` đặt `extra="forbid"` và chỉ có `status`/`error`.
        Tổng số dòng do control plane tự cộng dồn từ từng lô đã báo
        (`ingest_run.rows_written += body.rows` ở `/progress`) — gửi lại một tổng
        ở đây là mời hai con số nói khác nhau, và con số của pod thì không tính
        được những lô mà một lần chạy TRƯỚC đã ghi.
        """
        report = IngestCompletionReport(status=status, error=error)
        response = self._client.post(
            f"/internal/ingest/{self._run_id}/complete",
            json=report.model_dump(mode="json"),
        )
        response.raise_for_status()
