"""Logic của đường nạp. Không biết HTTP, không biết k8s — nhận `JobLauncher` vào.

Tách khỏi router để test được việc phân giải `secret_ref` mà không dựng cả ứng
dụng (xem `tests/test_ingest_service.py`: không database, không Docker), và để
`jobs.py` giữ được vị thế module DUY NHẤT chạm Kubernetes API — phép canh AST ở
`tests/test_k8s_client_guard.py` khẳng định đúng điều đó.

Vì lý do thứ hai, module này KHÔNG import `loom_api.jobs`: nó chỉ khai một
`Protocol` mô tả đúng phần bề mặt mà nó gọi tới. `JobLauncher` thật khớp
Protocol đó theo cấu trúc (mypy kiểm ở chỗ router truyền vào), còn double trong
test khớp mà không phải kéo theo `kubernetes` — thứ mà chính test này tồn tại
để không cần tới.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Protocol

# `k8s://<namespace>/<name>#<key>` — dạng DUY NHẤT dùng được ở local.
# `vault://` hợp lệ với `SECRET_REF_RE` (Giai đoạn 1) nhưng cụm local không tới
# được Vault, nên nó bị từ chối ở đây với thông báo nói đúng lý do.
#
# Hai lớp con của `SECRET_REF_RE` chứ không phải một bản chép: đây là tập CON
# hẹp hơn (chỉ `k8s://`, và có nhóm bắt để tách ba phần). Nới nó rộng ra bằng
# `SECRET_REF_RE` sẽ nhận lại `vault://` — đúng thứ hàm dưới đây tồn tại để
# chặn. `\Z` chứ không `$`, cùng lý do đã ghi ở `SECRET_REF_RE`: `$` khớp cả
# ngay trước một `\n` cuối chuỗi, và một tên Secret mang `\n` đi thẳng vào
# `metadata.name` của Job.
_K8S_REF = re.compile(
    r"\Ak8s://(?P<ns>[a-z0-9-]+)/(?P<name>[a-z0-9.-]+)#(?P<key>[A-Za-z0-9._-]+)\Z"
)


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


class JobLauncherLike(Protocol):
    """Đúng phần bề mặt của `loom_api.jobs.JobLauncher` mà đường nạp gọi tới.

    Tên tham số là một phần của hợp đồng, không phải trang trí: người gọi
    truyền `cpu`/`memory` bằng từ khoá để hai chuỗi tài nguyên không thể hoán
    vị cho nhau mà không ai thấy.
    """

    def launch(
        self,
        run_id: uuid.UUID,
        secret_name: str,
        shared_secret_ref: tuple[str, str],
        cpu: str,
        memory: str,
    ) -> None: ...


def resolve_secret_ref(secret_ref: str) -> SecretLocation:
    match = _K8S_REF.match(secret_ref)
    if match is None:
        if secret_ref.startswith("vault://"):
            raise SecretRefUnusable(
                "secret_ref dùng vault:// nhưng cụm này không tới được Vault; "
                "dùng k8s://<namespace>/<name>#<key> ở local"
            )
        raise SecretRefUnusable(f"secret_ref không dùng được: {secret_ref!r}")
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
            f"secret_ref trỏ vào namespace {location.namespace!r} nhưng Job nạp chạy ở "
            f"{task_namespace!r}; envFrom không chiếu được Secret qua namespace"
        )
    return location.name
