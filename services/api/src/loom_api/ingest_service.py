"""Phân giải `secret_ref` thành TÊN k8s Secret. Không biết HTTP, không biết k8s.

Tách khỏi router để test được việc phân giải mà không dựng cả ứng dụng (xem
`tests/test_ingest_service.py`: không database, không Docker, chạy trong
`make test`). Module này KHÔNG import `loom_api.jobs` — không vì phép canh AST
ở `tests/test_k8s_client_guard.py` (phép canh đó chỉ chặn `import kubernetes`,
và `routers/ingest.py` import `loom_api.jobs` một cách hợp lệ), mà vì nó không
cần: ở đây không có gì phóng Job.
"""

from __future__ import annotations

from dataclasses import dataclass

from loom_core.item_definitions import K8S_SECRET_REF_RE


class SecretRefUnusable(ValueError):
    """`secret_ref` đúng cú pháp Giai đoạn 1 nhưng cụm NÀY không dùng được nó.

    Tách khỏi `ValueError` trần để router dịch được thành 400 (yêu cầu của
    client sai, sửa được) mà không phải bắt mọi `ValueError` đi ngang qua —
    bắt rộng như thế sẽ nuốt luôn một lỗi lập trình và trả về 400 cho một sự
    cố của server.
    """


@dataclass(frozen=True, slots=True)
class SecretLocation:
    namespace: str
    name: str
    key: str


def resolve_secret_ref(secret_ref: str) -> SecretLocation:
    """Ba phần của một ref `k8s://`, hoặc `SecretRefUnusable` với lý do.

    `K8S_SECRET_REF_RE` là nhánh `k8s://` của `SECRET_REF_RE`, dựng từ CÙNG các
    lớp ký tự (xem `loom_core.item_definitions`) — không phải một bản chép, vì
    một bản chép trôi được và trôi ở đây nghĩa là từ chối một `secret_ref` mà
    `POST /items` đã nhận.
    """
    match = K8S_SECRET_REF_RE.match(secret_ref)
    if match is None:
        if secret_ref.startswith("vault://"):
            raise SecretRefUnusable(
                "secret_ref uses vault:// but this cluster cannot reach Vault; "
                "use k8s://<namespace>/<name>#<key> here"
            )
        # KHÔNG với tới được từ router: mọi giá trị lưu được đã qua
        # `SECRET_REF_RE` (`ConnectionDefinition._check_ref`), nên nó chỉ có
        # thể là `vault://` — bắt ở nhánh trên — hoặc `k8s://`, khớp ngay. Giữ
        # lại làm lớp phòng vệ cho người gọi trực tiếp (và cho ngày ai đó nới
        # `SECRET_REF_RE` ra); nói ra ở đây để không ai đi tìm cái request sinh
        # ra thông báo này.
        raise SecretRefUnusable(f"secret_ref is not usable: {secret_ref!r}")
    return SecretLocation(match["ns"], match["name"], match["key"])


def secret_name_for(secret_ref: str, task_namespace: str) -> str:
    """TÊN k8s Secret cho `envFrom` của pod nạp — và không gì hơn thế.

    Trả về một chuỗi chứ không phải `SecretLocation` là cố ý: đây là điểm cuối
    của đường đi, và thứ duy nhất được phép ra khỏi đây là cái tên. `key` không
    theo cùng vì `envFrom` chiếu TOÀN BỘ Secret vào pod (xem `JobLauncher.
    launch`) — nó không chọn khoá nào cả. `#<key>` trong ref vì vậy hiện là chú
    thích cho người đọc (khoá nào mang mật khẩu), không phải tham số; nói ra
    điều đó ở đây để không ai đi tìm chỗ nó "được dùng".

    `namespace` PHẢI trùng namespace nơi Job chạy. `envFrom` không vượt được
    namespace, mà `JobLauncher` luôn tạo Job trong `settings.task_namespace` —
    nên một ref trỏ sang namespace khác không phải "gần đúng": nó sinh ra một
    Job hỏi xin một Secret CÙNG TÊN trong namespace của Job, thứ thường không
    tồn tại. Kubernetes để pod kẹt ở `CreateContainerConfigError`, hàng
    `ingest_run` kẹt ở `pending`, và người vận hành đi tìm nguyên nhân ở chỗ
    hoàn toàn khác. Hỏng NGAY, tại đây, với một câu nói rõ namespace nào.
    """
    location = resolve_secret_ref(secret_ref)
    if location.namespace != task_namespace:
        raise SecretRefUnusable(
            f"secret_ref points at namespace {location.namespace!r} but the ingest Job runs "
            f"in {task_namespace!r}; envFrom cannot project a Secret across namespaces"
        )
    return location.name
