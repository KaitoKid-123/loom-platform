"""Quyết định của đường nạp mà KHÔNG cần HTTP và KHÔNG gọi Kubernetes.

Hai việc: phân giải `secret_ref` thành TÊN k8s Secret (Task 9), và đọc một
`JobStatus` ra thành "run này còn đường sống hay đã chết" (Task 13). Tách khỏi
router để test được cả hai mà không dựng cả ứng dụng (xem
`tests/test_ingest_service.py`: không database, không Docker, chạy trong
`make test`).

Module này CÓ import `loom_api.jobs`, và bản Task 9 thì không — nói ra vì
docstring cũ khẳng định điều ngược lại. Nó nhập đúng MỘT thứ: dataclass
`JobStatus`, tức là HÌNH DẠNG câu trả lời (bốn trường), không phải đường đi tới
Kubernetes. Không có gì ở đây mở kết nối, dựng `JobLauncher`, hay đọc
kubeconfig — `JobLauncher.__init__` là chỗ duy nhất làm việc đó. Phép canh AST ở
`tests/test_k8s_client_guard.py` chỉ chặn `import kubernetes` (và nó vẫn xanh:
import ở đây là `loom_api.jobs`), nên ràng buộc thật là ràng buộc ở trên: một
lời gọi mạng KHÔNG được xuất hiện trong file này.
"""

from __future__ import annotations

from dataclasses import dataclass

from loom_api.jobs import JobStatus
from loom_core.item_definitions import K8S_SECRET_REF_RE

# Hai trạng thái mà một run KHÔNG bao giờ rời khỏi nữa. Người gọi phải kiểm nó
# TRƯỚC khi hỏi Kubernetes — xem `failure_from_job` cho lý do điều kiện đó nằm ở
# chỗ gọi chứ không trong hàm.
TERMINAL_RUN_STATUSES = frozenset({"succeeded", "failed"})


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


def failure_from_job(job: JobStatus) -> str | None:
    """Lý do đánh run `failed`, hoặc `None` khi Job VẪN CÒN đường sống.

    **Mặc định là ĐỂ NGUYÊN.** Chỉ ba hình dạng Job dưới đây trả về một lý do;
    mọi hình dạng khác — kể cả những hình dạng chưa ai gặp — cho ra `None`. Đó
    là hướng an toàn của hai hướng: để một run chết nằm lại thêm một lượt đọc là
    một thanh tiến trình chạy lâu hơn cần thiết, còn đánh `failed` một run đang
    sống là giết công việc của người dùng bằng một phỏng đoán.

    HAI trạng thái LÀNH MẠNH mà mặc định đó phục vụ, và cả hai đều thường gặp:

    - `exists=True, active=1` trên một run `pending` là khoảng giữa "Job vừa
      được tạo" và "pod gọi `/spec` lần đầu". MỌI lần nạp đi qua nó.
    - `exists=True, active=1` trên một run `running` là một pod đang chạy. Không
      có heartbeat nào trong Giai đoạn 3a, nên `active` LÀ tín hiệu sống.

    Vì cả hai chỉ cần mặc định, hàm này không nhận `run_status`: không có nhánh
    nào phân biệt `pending` với `running` cả. Điều kiện "chỉ đối chiếu run CHƯA
    kết thúc" (`TERMINAL_RUN_STATUSES`) nằm ở CHỖ GỌI chứ không ở đây, và cố ý:
    nó phải chặn TRƯỚC lời gọi Kubernetes — một run đã xong thì Job của nó đã bị
    TTL dọn (`ttl_seconds_after_finished=3600`), nên hỏi thêm vừa vô nghĩa vừa
    trả về đúng `exists=False`, thứ sẽ biến `succeeded` thành `failed` sau một
    giờ. Đặt điều kiện đó vào đây cũng được, nhưng khi ấy vẫn còn một round trip
    tới cụm cho mỗi lần đọc một run đã đóng, và tính chất "không ai hỏi k8s về
    một run đã kết thúc" không còn kiểm được từ ngoài.

    **KHÔNG suy ra `succeeded`.** `succeeded=1` trên Job mà hàng chưa kết thúc là
    một run `failed`, không phải một run thành công. `/complete` là nguồn sự thật
    DUY NHẤT cho việc run kết thúc ĐÚNG, và `rows_written` chỉ tiến qua
    `/progress` — nên một "thành công" không ai xác nhận là một con số ta không
    có. Đây là hình dạng của một pod bị OOMKill ngay sau lô cuối: container thoát
    0 ở lần thử duy nhất, nhưng lời báo cuối không bao giờ đi.

    Thông báo trả về đi thẳng vào `ingest_run.error` và hiện lên cho người dùng,
    nên nó phải tự đủ nghĩa: pod đã bị dọn từ lâu khi có người đọc tới, và hàng
    này là thứ duy nhất còn lại.
    """
    if not job.exists:
        return (
            "the Kubernetes Job for this run no longer exists and the run was never closed by "
            "its pod — either the Job was never created, or it finished long enough ago to be "
            "cleaned up (Jobs are kept for one hour after they finish). Nothing further will "
            "be reported for this run; start a new ingest."
        )
    if job.failed:
        return (
            "the Kubernetes Job for this run has a failed pod and the run was never closed by "
            "that pod — a wrong Secret name keeps the pod from ever starting, and the ingest "
            "is not retried automatically. Check the pod's logs (kept for one hour after the "
            "Job finishes), then start a new ingest."
        )
    if job.succeeded:
        return (
            "the Kubernetes Job for this run finished but the pod never reported the outcome, "
            "so how far it got is unknown — the rows counted here are only the ones it "
            "reported. Treat the target table as incomplete and start a new ingest."
        )
    return None
