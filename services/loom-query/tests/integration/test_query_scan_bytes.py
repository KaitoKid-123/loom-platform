"""Giới hạn 2 (Task 8) — trần byte quét, kiểm TRƯỚC khi đọc dữ liệu.

**Thăm dò PyIceberg (KIỂM TRƯỚC KHI XÂY, bắt buộc theo spec):**
`test_pyiceberg_exposes_per_file_byte_stats_via_plan_files` in nguyên văn thứ
`Table.scan().plan_files()` phơi ra trên PyIceberg 0.11.1 thật, với Lakekeeper
thật — không đoán. Xem thêm docstring `loom_iceberg.Lakehouse.scan_size_bytes`.

**Chứng minh đỏ 1 của Task 8** (bắt buộc, ghi trong báo cáo hoàn tất task):
chuyển lời gọi `check_scan_bytes(...)` trong `runner._run_sync` xuống SAU vòng
lặp đăng ký bảng (tức là SAU khi `Lakehouse.scan()` đã chạy cho mọi bảng), rồi
chạy lại `test_scan_over_the_byte_cap_is_rejected_before_any_table_is_scanned`.
Phép kiểm phải ĐỎ, và phải đỏ ở dòng `assert scanned_qualified == []` — nghĩa
là một scan THẬT đã chạy trước khi bị từ chối — KHÔNG đỏ ở khẳng định
"status == failed"/nội dung lỗi (những khẳng định đó vẫn đúng dù kiểm trước
hay sau, vì `check_scan_bytes` vẫn ném cùng một lỗi cuối cùng). Đây là cách
phân biệt "đỏ vì đã có byte bị quét" với "đỏ vì thông báo lỗi khác đi" mà spec
yêu cầu: hai khẳng định độc lập, một khẳng định hành vi (bị từ chối), một
khẳng định THỨ TỰ (chưa từng scan).
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from loom_core.schemas import Principal
from loom_iceberg import Lakehouse, build_catalog
from loom_query.config import Settings
from loom_query.main import create_app

from ..conftest import FakeAuthz

# `fake_authz`/`principal` (tests/conftest.py) và `app_settings`/`seeded_table`/
# `lakehouse_id`/`lakekeeper`/`s3_endpoint`/`warehouse_name` (tests/integration/
# conftest.py) là fixture, tiêm thẳng theo tên tham số — không cần import.

pytestmark = pytest.mark.integration


def _body(lakehouse_id: uuid.UUID, sql: str, principal: Principal) -> dict[str, Any]:
    return {
        "lakehouse_id": str(lakehouse_id),
        # Mọi câu SQL ở file này dùng bảng HAI phần — `run_gate` không hỏi gì
        # về workspace cho trường hợp đó, nên một UUID ngẫu nhiên là đủ.
        "workspace_id": str(uuid.uuid4()),
        "sql": sql,
        "principal": {
            "user_id": str(principal.user_id),
            "subject": principal.subject,
            "email": principal.email,
            "display_name": principal.display_name,
            "groups": list(principal.groups),
        },
    }


async def _run_and_wait(
    app: FastAPI, lakehouse_id: uuid.UUID, sql: str, principal: Principal
) -> dict[str, Any]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        create_response = await client.post(
            "/api/v1/query", json=_body(lakehouse_id, sql, principal)
        )
        assert create_response.status_code == 202, create_response.text
        query_id = create_response.json()["query_id"]

        body: dict[str, Any] = {}
        for _ in range(200):
            status_response = await client.get(f"/api/v1/query/{query_id}")
            body = status_response.json()
            if body["status"] != "running":
                break
            await asyncio.sleep(0.05)
    return body


def test_pyiceberg_exposes_per_file_byte_stats_via_plan_files(
    lakekeeper: str,
    s3_endpoint: str,
    warehouse_name: str,
    seeded_table: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Thăm dò bắt buộc trước khi xây `check_scan_bytes`/`scan_size_bytes`.

    `sales.orders` (fixture `seeded_table`) có ba dòng, MỘT data file. In
    nguyên văn `FileScanTask.file.file_size_in_bytes` — đây là con số dùng
    làm trần, lấy thẳng từ manifest, KHÔNG mở file parquet để đo.
    """
    catalog = build_catalog(
        catalog_uri=f"{lakekeeper}/catalog", warehouse=warehouse_name, s3_endpoint=s3_endpoint
    )
    table = catalog.load_table(seeded_table)
    tasks = list(table.scan().plan_files())

    with capsys.disabled():
        print(f"\n\n=== Thăm dò PyIceberg 0.11.1 — plan_files() trên {seeded_table} ===")
        print(f"table.scan() -> {type(table.scan()).__name__}")
        print(f"table.scan().plan_files() -> {type(tasks[0]).__name__} x {len(tasks)}")
        for task in tasks:
            print(f"  FileScanTask.file -> {type(task.file).__name__}")
            print(f"    .file_path           = {task.file.file_path}")
            print(f"    .file_size_in_bytes  = {task.file.file_size_in_bytes}")
            print(f"    .record_count        = {task.file.record_count}")
        total = sum(t.file.file_size_in_bytes for t in tasks)
        print(f"Tổng file_size_in_bytes (dùng làm 'byte quét'): {total}")
        via_helper = Lakehouse(catalog).scan_size_bytes(seeded_table)
        print(f"Lakehouse.scan_size_bytes({seeded_table!r}) = {via_helper}")
        print("=== hết thăm dò ===\n")

    assert tasks, "bảng có dữ liệu phải cho ít nhất một FileScanTask"
    assert all(t.file.file_size_in_bytes > 0 for t in tasks)
    assert via_helper == total


async def test_scan_over_the_byte_cap_is_rejected_before_any_table_is_scanned(
    app_settings: Settings,
    seeded_table: str,
    lakehouse_id: uuid.UUID,
    fake_authz: FakeAuthz,
    principal: Principal,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`max_scan_bytes=1` — chắc chắn nhỏ hơn BẤT KỲ file parquet thật nào
    (footer parquet đã vài trăm byte), nên phép từ chối tất định, không cần
    biết trước kích thước thật của `seeded_table`.
    """
    scanned_qualified: list[str] = []
    original_scan = Lakehouse.scan

    def spying_scan(self: Lakehouse, qualified: str) -> Any:
        scanned_qualified.append(qualified)
        return original_scan(self, qualified)

    monkeypatch.setattr(Lakehouse, "scan", spying_scan)

    fake_authz.grant(lakehouse_id, "viewer")
    tiny_cap = app_settings.model_copy(update={"max_scan_bytes": 1})
    app = create_app(settings=tiny_cap, authz=fake_authz)

    body = await _run_and_wait(app, lakehouse_id, "SELECT id, amount FROM sales.orders", principal)

    assert body["status"] == "failed", body
    assert "byte cap" in body["error"], body["error"]
    # Đây là khẳng định CỐT LÕI của "kiểm TRƯỚC khi quét": nếu `check_scan_bytes`
    # chạy sau vòng lặp đăng ký bảng, `scanned_qualified` sẽ khác rỗng ở đây dù
    # khẳng định "status == failed" phía trên vẫn đúng — xem docstring module.
    assert scanned_qualified == [], (
        f"Lakehouse.scan() đã được gọi cho {scanned_qualified} TRƯỚC khi bị từ "
        "chối — trần byte quét không còn chặn trước khi đọc dữ liệu"
    )


async def test_scan_at_or_under_the_cap_succeeds(
    app_settings: Settings,
    seeded_table: str,
    lakehouse_id: uuid.UUID,
    lakekeeper: str,
    s3_endpoint: str,
    warehouse_name: str,
    fake_authz: FakeAuthz,
    principal: Principal,
) -> None:
    """Biên trên: cap ĐÚNG BẰNG kích thước thật thì KHÔNG bị từ chối — trần là
    "tối đa", không phải "nhỏ hơn". Cap lấy từ chính `scan_size_bytes` thật,
    không phải một hằng số đoán."""
    catalog = build_catalog(
        catalog_uri=f"{lakekeeper}/catalog", warehouse=warehouse_name, s3_endpoint=s3_endpoint
    )
    real_size = Lakehouse(catalog).scan_size_bytes(seeded_table)

    fake_authz.grant(lakehouse_id, "viewer")
    exact_cap = app_settings.model_copy(update={"max_scan_bytes": real_size})
    app = create_app(settings=exact_cap, authz=fake_authz)

    body = await _run_and_wait(
        app, lakehouse_id, "SELECT id, amount FROM sales.orders ORDER BY id", principal
    )

    assert body["status"] == "succeeded", body
    assert body["rows"] == [[1, 10.0], [2, 20.0], [3, 30.0]]
