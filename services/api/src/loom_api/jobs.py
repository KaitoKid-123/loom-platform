"""Chỗ DUY NHẤT trong `loom-api` chạm Kubernetes API.

Giai đoạn 1 xây `loom-api` như một control plane chỉ đọc Postgres. Giai đoạn 3a
cho nó tạo `Job` để chạy tác vụ nạp dữ liệu — món nợ thứ HAI cùng loại với
credential gốc MinIO ở `warehouse_provisioning.py` (xem docstring module đó và
`tests/test_root_credential_guard.py`). Chủ dự án chấp nhận với cùng điều kiện:
phạm vi phải hẹp nhất có thể — ĐÚNG MỘT module — và phải CANH ĐƯỢC, không chỉ là
một đoạn văn. `tests/test_k8s_client_guard.py` đọc AST của mọi module trong
`loom_api` và khẳng định đúng module này (và chỉ nó) `import kubernetes`.

**Tên Job là `f"ingest-{run_id}"`, và đó là một quyết định về ĐỘ BỀN, không phải
một quy ước đặt tên.** `run_id` đã là khoá chính của `IngestRun` trong Postgres
(xem `models.py`), nên đặt tên Job tất định theo nó làm việc SUBMIT LẠI trở nên
VÔ HẠI: gọi `launch()` hai lần với cùng `run_id` — dù vì người dùng bấm hai lần
hay vì `loom-api` khởi động lại giữa chừng và không nhớ đã phóng Job hay chưa —
luôn cho ra ĐÚNG MỘT Job, không bao giờ hai pod cùng ghi cho một run. Đây chính
là tính chất mà spec v1 gọi là "trạng thái mong muốn nằm trong Postgres cộng một
vòng lặp đối chiếu": Postgres giữ *ý định* (hàng `ingest_run`), Kubernetes chỉ
cần được yêu cầu lại tới khi ý định đó thành sự thật, và yêu cầu lại không bao
giờ nhân đôi tác dụng phụ.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol

from kubernetes import client, config
from kubernetes.client.rest import ApiException


class JobLauncherLike(Protocol):
    """Đúng phần bề mặt của `JobLauncher` mà đường nạp (Task 9) gọi tới.

    Ở CẠNH lớp nó mô tả, không ở module người gọi: một Protocol đặt xa lớp thật
    là hai khai báo phải giữ khớp nhau bằng trí nhớ. `JobLauncher` khớp theo
    cấu trúc, và mypy kiểm điều đó tại chỗ gán trong `routers/ingest.py`.

    Lý do nó tồn tại là để TEST thay được: double trong
    `tests/integration/test_ingest_api.py` không dựng nổi một `JobLauncher`
    thật (constructor nạp kubeconfig). Nó KHÔNG tồn tại vì phép canh AST ở
    `tests/test_k8s_client_guard.py` — phép canh đó chỉ chặn `import
    kubernetes`, và import chính module này thì hợp lệ ở mọi nơi.

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


def job_name(run_id: uuid.UUID) -> str:
    """Tên Job tất định — xem lý do ĐỘ BỀN ở docstring module."""
    return f"ingest-{run_id}"


@dataclass(frozen=True, slots=True)
class JobStatus:
    """Trạng thái đọc lại từ một `Job` k8s, hoặc bằng chứng nó chưa hề tồn tại.

    `active`/`succeeded`/`failed` là BA BỘ ĐẾM NGUYÊN riêng biệt của
    `.status.*`, KHÔNG phải một enum "trạng thái" duy nhất — xem lý do ở
    `JobLauncher.status`.
    """

    exists: bool
    active: int = 0
    succeeded: int = 0
    failed: int = 0


class JobLauncher:
    """Bọc `BatchV1Api` — người gọi (Task 9) không cần biết gì về k8s ngoài
    `run_id`, tên Secret nguồn, và bí mật chia sẻ."""

    def __init__(self, namespace: str, image: str, api_base_url: str) -> None:
        # Thử nạp cấu hình trong-cụm trước (pod thật chạy trong k8s luôn có
        # ServiceAccount token mounted sẵn); rơi về kubeconfig cho máy dev đứng
        # ngoài cụm. Bắt ĐÚNG `config.ConfigException` — bắt `Exception` trần ở
        # đây sẽ nuốt luôn lỗi cấu hình THẬT (kubeconfig hỏng, quyền file sai...)
        # và biến nó thành một lần rơi-về gây rối, khó phân biệt với trường hợp
        # "không chạy trong cụm" hợp lệ.
        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()
        self._batch = client.BatchV1Api()
        self._namespace = namespace
        self._image = image
        self._api_base_url = api_base_url

    def launch(
        self,
        run_id: uuid.UUID,
        secret_name: str,
        shared_secret_ref: tuple[str, str],
        cpu: str,
        memory: str,
    ) -> None:
        """Dựng và tạo `Job` nạp cho `run_id`. Idempotent theo tên (xem
        `job_name`) — gọi lại sau một 409 là vô hại và có chủ đích.
        """
        shared_secret_name, shared_secret_key = shared_secret_ref
        job = client.V1Job(
            metadata=client.V1ObjectMeta(
                name=job_name(run_id),
                labels={"app": "loom-task", "run-id": str(run_id)},
            ),
            spec=client.V1JobSpec(
                # KHÔNG thử lại tự động. Đo 1 (Task 1) đã ăn đúng bẫy này: thiếu
                # `backoff_limit=0`, một Job nạp hỏng bị Kubernetes tự thử lại
                # (mặc định 6 lần), và mỗi lần thử lại tạo warehouse Lakekeeper
                # riêng — để lại NHIỀU warehouse mồ côi từ một lần chạy hỏng.
                # Thử lại còn ĐÚNG về mặt dữ liệu (mỗi lần chạy đọc lại watermark
                # từ Postgres, xem docstring `job_name` ở trên) — chính điều đó
                # làm nó nguy hiểm: ba lần thử âm thầm giấu mất một lỗi cấu hình
                # (secret sai tên, nguồn không nối được...) đằng sau vẻ ngoài
                # "cuối cùng cũng chạy". Nạp lại ở Giai đoạn 3a là một hành động
                # CHỦ ĐỘNG của người dùng, không phải việc của Kubernetes.
                backoff_limit=0,
                # Dọn Job sau 1 giờ kể từ khi kết thúc (thành công hay thất
                # bại): đủ lâu để một người vào xem log lúc hỏng (thường phát
                # hiện trong vài phút tới vài chục phút, không phải ngay lập
                # tức), đủ ngắn để Job không chất đống trong namespace qua
                # nhiều lần nạp một ngày.
                ttl_seconds_after_finished=3600,
                template=client.V1PodTemplateSpec(
                    metadata=client.V1ObjectMeta(labels={"app": "loom-task"}),
                    spec=client.V1PodSpec(
                        restart_policy="Never",
                        containers=[
                            client.V1Container(
                                name="task",
                                image=self._image,
                                env=[
                                    client.V1EnvVar("LOOM_TASK_RUN_ID", str(run_id)),
                                    client.V1EnvVar("LOOM_TASK_API_BASE_URL", self._api_base_url),
                                    # Bí mật chia sẻ cho `/internal/ingest/*`
                                    # (cùng khuôn header+`hmac.compare_digest`
                                    # đã dùng ở Giai đoạn 2b cho loom-query) —
                                    # chứng minh với loom-api rằng request tới
                                    # TỪ pod nạp của đúng run này.
                                    client.V1EnvVar(
                                        "LOOM_TASK_SHARED_SECRET",
                                        value_from=client.V1EnvVarSource(
                                            secret_key_ref=client.V1SecretKeySelector(
                                                name=shared_secret_name,
                                                key=shared_secret_key,
                                            )
                                        ),
                                    ),
                                ],
                                # Credential Postgres NGUỒN vào thẳng pod qua
                                # `envFrom` bằng TÊN Secret — `loom-api` không
                                # bao giờ đọc giá trị bên trong. Đây là lời hứa
                                # mà `ConnectionDefinition.secret_ref` tồn tại
                                # để giữ (xem `SECRET_REF_RE` ở
                                # `loom_core.item_definitions`): secret_ref chỉ
                                # là một ĐƯỜNG DẪN, không phải mật khẩu, nên một
                                # lỗi SQL injection trong API không có gì để rò
                                # rỉ — nó chỉ đọc được TÊN, chưa từng thấy giá
                                # trị.
                                env_from=[
                                    client.V1EnvFromSource(
                                        secret_ref=client.V1SecretEnvSource(name=secret_name)
                                    )
                                ],
                                resources=client.V1ResourceRequirements(
                                    requests={"cpu": cpu, "memory": memory},
                                    limits={"memory": memory},
                                ),
                            )
                        ],
                    ),
                ),
            ),
        )
        try:
            self._batch.create_namespaced_job(self._namespace, job)
        except ApiException as exc:
            # 409 nghĩa là "Job này đã có rồi" — câu trả lời ĐÚNG cho một lần
            # submit lại (xem docstring `job_name`). CHỈ nuốt đúng 409: mọi mã
            # khác (403 vì Role thiếu quyền, 500 vì API server lỗi...) phải
            # NỔI LÊN. Nuốt luôn 403 sẽ giấu mất một Role thiếu quyền phía sau
            # một lần "gọi thành công" giả — và triệu chứng lộ ra sẽ là run kẹt
            # mãi ở `pending` (xem docstring `IngestRun.status` ở models.py),
            # rất xa nguyên nhân thật.
            if exc.status != 409:
                raise

    def status(self, run_id: uuid.UUID) -> JobStatus:
        """404 nghĩa là `exists=False` — không phải lỗi, đó là câu trả lời hợp
        lệ cho "Job của run này đã bị dọn hoặc chưa từng được tạo". Mọi mã khác
        phải nổi lên, không được nuốt.

        **Đọc số đếm nguyên `.status.active`/`.succeeded`/`.failed`, TUYỆT ĐỐI
        KHÔNG so khớp chuỗi trên `.status.conditions`.** Bẫy đã ăn thật trong
        giai đoạn này (xem chú thích ở target `probe-single-commit` trong
        `Makefile`): trên k3s 1.32.13 — bản cụm này chạy — một Job đã Complete
        mang HAI condition cùng `status: "True"` (`SuccessCriteriaMet` VÀ
        `Complete`, từ tính năng JobSuccessPolicy). Bất kỳ logic nào so khớp
        chuỗi trên điều kiện đó (`jsonpath` kiểu
        `conditions[?(@.status=="True")].type`, hay tương đương trong Python)
        sẽ vỡ ngay khi hai chuỗi đó nối lại thành một giá trị không khớp gì cả.
        Ba số đếm nguyên ổn định qua các bản k8s và không có điều kiện phụ nào
        cần so khớp.
        """
        try:
            job = self._batch.read_namespaced_job_status(job_name(run_id), self._namespace)
        except ApiException as exc:
            if exc.status == 404:
                return JobStatus(exists=False)
            raise
        status = job.status
        return JobStatus(
            exists=True,
            active=status.active or 0,
            succeeded=status.succeeded or 0,
            failed=status.failed or 0,
        )
