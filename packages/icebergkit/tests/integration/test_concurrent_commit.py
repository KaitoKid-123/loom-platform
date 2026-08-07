"""Cửa chặn của giai đoạn: commit đồng thời trên Lakekeeper THẬT.

Lakekeeper GIỮ khoá — không phải PyIceberg giữ hộ trong tiến trình Python. Đó
là lý do duy nhất Giai đoạn 2 chọn Lakekeeper thay vì `SqlCatalog` của
PyIceberg, và đây là phép kiểm khẳng định đúng điều đó.

Mỗi writer dựng `RestCatalog` RIÊNG của chính nó — dùng chung một đối tượng
catalog thì phép này chỉ kiểm được khoá TRONG TIẾN TRÌNH (một `threading.Lock`
ai đó lỡ thêm vào, chẳng hạn), không kiểm được khoá PHÍA LAKEKEEPER. Đây cũng
đúng hình dạng Giai đoạn 2b: mỗi query một pod, mỗi pod một catalog.
"""

import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

import pyarrow as pa
import pytest

from loom_iceberg import Lakehouse, build_catalog

pytestmark = pytest.mark.integration

WRITERS = 8


def _write_one(
    lakekeeper: str, s3_endpoint: str, warehouse_name: str, qualified: str, n: int
) -> tuple[int, str | None]:
    """Một writer, catalog RIÊNG, một dòng. Trả `(n, None)` nếu thành công,
    `(n, mo_ta_loi)` nếu không — bắt MỌI lỗi, không chỉ loại đoán trước, vì
    điều cần đếm là "commit này còn nguyên hay không", không phải một lớp lỗi
    cụ thể."""
    try:
        catalog = build_catalog(
            catalog_uri=f"{lakekeeper}/catalog",
            warehouse=warehouse_name,
            s3_endpoint=s3_endpoint,
        )
        data = pa.table({"writer": pa.array([n], type=pa.int64())})
        Lakehouse(catalog).append(qualified, data)
        return n, None
    except Exception as exc:
        return n, f"{type(exc).__name__}: {exc}"


def _run_writers(
    lakekeeper: str, s3_endpoint: str, warehouse_name: str, qualified: str
) -> list[tuple[int, str | None]]:
    with ThreadPoolExecutor(max_workers=WRITERS) as pool:
        futures = [
            pool.submit(_write_one, lakekeeper, s3_endpoint, warehouse_name, qualified, n)
            for n in range(WRITERS)
        ]
        return [future.result() for future in as_completed(futures)]


@pytest.fixture
def seeded_table(lakehouse: Lakehouse) -> str:
    """Một bảng với đúng MỘT hàng seed, trước khi tám writer xông vào."""
    qualified = f"conc_{uuid.uuid4().hex[:8]}.t1"
    namespace, _, _ = qualified.partition(".")
    lakehouse.create_namespace(namespace)
    lakehouse.create_from(qualified, pa.table({"writer": pa.array([-1], type=pa.int64())}))
    return qualified


def test_concurrent_appends_do_not_lose_commits(
    lakekeeper: str,
    s3_endpoint: str,
    warehouse_name: str,
    lakehouse: Lakehouse,
    seeded_table: str,
) -> None:
    """Số dòng cuối phải bằng 1 (seed) + số writer THÀNH CÔNG.

    Chênh lệch nghĩa là một commit BÁO thành công rồi biến mất — Lakekeeper
    xác nhận ghi nhưng dữ liệu không còn trong snapshot cuối. Đây là lỗi
    nghiêm trọng nhất có thể xảy ra ở một catalog: mất dữ liệu trong im lặng.
    """
    results = _run_writers(lakekeeper, s3_endpoint, warehouse_name, seeded_table)
    errors = [(n, err) for n, err in results if err is not None]
    oks = [n for n, err in results if err is None]

    total_rows = lakehouse.scan(seeded_table).read_all().num_rows
    expected = 1 + len(oks)
    assert total_rows == expected, (
        f"tong dong doc lai = {total_rows}, ky vong = {expected} "
        f"(1 hang seed + {len(oks)}/{WRITERS} writer bao thanh cong); "
        f"cac writer loi: {errors}"
    )


def test_a_rejected_writer_fails_loudly(
    lakekeeper: str,
    s3_endpoint: str,
    warehouse_name: str,
    lakehouse: Lakehouse,
    seeded_table: str,
) -> None:
    """Xung đột commit được PHÉP xảy ra; im lặng thì không.

    Không có phép này, một catalog nuốt HẾT mọi commit (mọi writer đều lỗi)
    vẫn làm `test_concurrent_appends_do_not_lose_commits` xanh: số lỗi sẽ
    bằng `WRITERS`, số dòng mong đợi tụt xuống 1 (chỉ còn hàng seed), và 1
    KHỚP 1. Phép này chặn đúng lỗ hổng đó bằng cách đòi ít nhất một writer
    THẬT SỰ thành công.
    """
    results = _run_writers(lakekeeper, s3_endpoint, warehouse_name, seeded_table)
    errors = [(n, err) for n, err in results if err is not None]
    oks = [n for n, err in results if err is None]

    assert oks, f"khong writer nao thanh cong trong so {WRITERS} — cac loi: {errors}"

    total_rows = lakehouse.scan(seeded_table).read_all().num_rows
    expected = 1 + len(oks)
    assert total_rows == expected, (
        f"tong dong doc lai = {total_rows}, ky vong = {expected} "
        f"(1 hang seed + {len(oks)}/{WRITERS} writer bao thanh cong); "
        f"cac writer loi: {errors}"
    )
