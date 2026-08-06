"""Bootstrap Lakekeeper và tạo warehouse.

Không có trong plan Giai đoạn 2a gốc: plan không biết rằng một Lakekeeper mới
dựng phải được BOOTSTRAP (đặt admin ban đầu) trước khi bất kỳ API nào khác
dùng được, và rằng `GET /catalog/v1/config` trả HTTP 400 nếu thiếu tham số
`warehouse` — nghĩa là phải có warehouse trước khi `RestCatalog` (trong
`catalog.py`) gọi được bất cứ thứ gì.
"""

import httpx

# MinIO BỎ QUA trường này (giống `_UNUSED_ROLE_ARN` của storagekit), nhưng
# Lakekeeper đòi nó có hình dạng một ARN hợp lệ để chấp nhận body tạo warehouse.
_UNUSED_ROLE_ARN = "arn:aws:iam::000000000000:role/loom"

_TIMEOUT_SECONDS = 30.0


def ensure_bootstrapped(management_url: str) -> None:
    """Bootstrap Lakekeeper nếu chưa, và không hỏng nếu gọi lần hai.

    `POST /management/v1/bootstrap` trả 400 `CatalogAlreadyBootstrapped` nếu
    catalog đã bootstrap — nên hàm này đọc `GET /management/v1/info` trước,
    đúng trường `bootstrapped`, và chỉ POST khi trường đó còn `false`. Không
    có bước đọc trước, mọi test thứ hai trong cùng session (dùng chung một
    Lakekeeper) sẽ đỏ vì lỗi bootstrap-lần-hai, dù không hề liên quan tới thứ
    test đó đang kiểm.
    """
    with httpx.Client(timeout=_TIMEOUT_SECONDS) as client:
        info = client.get(f"{management_url}/management/v1/info")
        info.raise_for_status()
        if info.json()["bootstrapped"]:
            return
        response = client.post(
            f"{management_url}/management/v1/bootstrap",
            json={"accept-terms-of-use": True},
        )
        response.raise_for_status()


def create_warehouse(
    management_url: str,
    *,
    name: str,
    bucket: str,
    key_prefix: str,
    s3_endpoint: str,
    access_key: str,
    secret_key: str,
) -> str:
    """Tạo warehouse với `sts-enabled=true` và trả về `warehouse-id`.

    Đây là chế độ credential DUY NHẤT mà `PyArrowFileIO` của PyIceberg dùng
    được thẳng, không cần thêm `s3fs` — xem bảng hai chế độ trong docstring
    của `catalog.py`. `remote-signing-enabled` bị Lakekeeper BỎ QUA ÂM THẦM
    (HTTP 201 nhưng profile lưu lại không có trường đó), nên không có trong
    body dưới đây — đã kiểm bằng thực nghiệm trên Lakekeeper v0.9.2, không
    lặp lại phép thử đó.

    `access_key`/`secret_key` ở đây là credential GỐC của MinIO, dùng để
    Lakekeeper tự AssumeRole hộ — không phải credential mà client (PyIceberg)
    nhận được. Client không bao giờ nhìn thấy cặp này.
    """
    body = {
        "warehouse-name": name,
        "storage-profile": {
            "type": "s3",
            "bucket": bucket,
            "key-prefix": key_prefix,
            "endpoint": s3_endpoint,
            "region": "us-east-1",
            "path-style-access": True,
            "flavor": "s3-compat",
            "sts-enabled": True,
            "sts-role-arn": _UNUSED_ROLE_ARN,
        },
        "storage-credential": {
            "type": "s3",
            "credential-type": "access-key",
            "aws-access-key-id": access_key,
            "aws-secret-access-key": secret_key,
        },
    }
    with httpx.Client(timeout=_TIMEOUT_SECONDS) as client:
        response = client.post(f"{management_url}/management/v1/warehouse", json=body)
        response.raise_for_status()
    warehouse_id = response.json()["warehouse-id"]
    if not isinstance(warehouse_id, str):
        raise TypeError(f"warehouse-id không phải string: {warehouse_id!r}")
    return warehouse_id
