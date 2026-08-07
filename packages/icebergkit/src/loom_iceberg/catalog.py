"""Dựng `RestCatalog` nói với Lakekeeper THẬT — không tự mang credential.

Một warehouse của Lakekeeper chỉ có HAI chế độ credential, không có chế độ thứ
ba "client tự mang access key" (đã xác minh trên Lakekeeper v0.9.2 chạy thật):

    sts-enabled=false  Lakekeeper ký hộ từng request (remote signing)
                        -> PyIceberg cần fsspec, và fsspec cần thêm `s3fs`.
    sts-enabled=true   Lakekeeper cấp credential STS hẹp theo `key-prefix`
                        -> PyIceberg dùng PyArrowFileIO, không cần gì thêm.

Package này dùng chế độ `sts-enabled=true` (xem `warehouse.py`), nên
`build_catalog` KHÔNG truyền `s3.access-key-id`/`s3.secret-access-key`: bất kỳ
giá trị nào truyền vào cũng bị Lakekeeper cấp credential STS đè lên ngay lần
gọi API đầu tiên. Truyền credential vào đây chỉ tạo cảm giác an toàn giả.
"""

from pyiceberg.catalog.rest import RestCatalog


def build_catalog(*, catalog_uri: str, warehouse: str, s3_endpoint: str) -> RestCatalog:
    """Dựng một `RestCatalog` trỏ tới Lakekeeper.

    `s3.path-style-access` LUÔN là `"true"`: MinIO chỉ nói path-style
    (`http://endpoint/bucket/key`), không nói virtual-host style
    (`http://bucket.endpoint/key`). Thiếu cờ này, PyIceberg dựng nhầm URL
    virtual-host, và lỗi hiện ra là timeout kết nối — không hề nhắc gì tới
    style, nên đây là một chi tiết dễ quên và khó truy ngược.

    Mỗi lời gọi trả một `RestCatalog` MỚI, không chia sẻ. Giai đoạn 2b mỗi
    query dựng catalog riêng của chính nó — xem `Lakehouse` và
    `tests/integration/test_concurrent_commit.py`.
    """
    return RestCatalog(
        name="loom",
        **{
            "uri": catalog_uri,
            "warehouse": warehouse,
            "s3.endpoint": s3_endpoint,
            "s3.path-style-access": "true",
        },
    )
