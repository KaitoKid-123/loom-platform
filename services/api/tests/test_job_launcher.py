"""`JobLauncher` — kiểm bằng một double TỰ VIẾT, không phải `unittest.mock`.

`MagicMock` chấp nhận MỌI lệnh gọi, kể cả gọi sai tên phương thức hay sai thứ
tự tham số — nó không giữ được hợp đồng nào, chỉ ghi nhận "có gì đó được gọi".
`_FakeBatchV1Api` dưới đây chỉ hiểu ĐÚNG hai phương thức mà `JobLauncher` thật
sự gọi (`create_namespaced_job`, `read_namespaced_job`), với đúng chữ ký của
`BatchV1Api` thật (`kubernetes` 36.x) — gọi sai tên hay sai thứ tự tham số sẽ
ném `TypeError`/`AttributeError` ngay, đúng như dùng nhầm client thật, thay vì
âm thầm "thành công" như một `MagicMock` sẽ làm.

Việc double CỐ Ý không có `read_namespaced_job_status` là một phép canh, không
phải một chỗ chưa viết tới: hai phương thức đó trả về cùng một `V1Job`, nhưng
phương thức có hậu tố `_status` gọi vào subresource `jobs/status` — một resource
RBAC KHÁC mà Role của `loom-api` không cấp. Quay lại dùng nó thì mọi bài dưới
đây ném `AttributeError` ở đây, thay vì xanh hết rồi trả 500 trên cụm thật. Xem
docstring `JobLauncher.status` cho thông báo 403 thật đã gặp.

Không có `JobLauncher` nào ở đây từng chạm mạng hay cụm thật: fixture
`fake_batch_api` monkeypatch `loom_api.jobs.client.BatchV1Api` thành double này,
và `loom_api.jobs.config.load_incluster_config` thành no-op — máy chạy test
không cần kubeconfig, không cần ở trong cụm.
"""

import uuid

import pytest
from kubernetes import client
from kubernetes.client.rest import ApiException

from loom_api import jobs
from loom_api.jobs import JobLauncher, JobStatus, job_name

RUN_ID = uuid.UUID("6e6f6e63-0000-4000-8000-0000000000a1")


class _FakeBatchV1Api:
    """Recording double thay cho `kubernetes.client.BatchV1Api` — xem docstring
    module cho lý do không dùng `MagicMock`.

    Tự mô phỏng đúng MỘT hành vi thật của k8s mà bài test "submit lại" cần:
    tạo một Job trùng tên là 409, không phải ghi đè âm thầm. Mọi mã lỗi khác
    (403, 500, ...) không tự nhiên sinh ra được từ trạng thái nội bộ của double
    — double không có khái niệm RBAC để tự suy luận ra 403 — nên phải bơm từ
    ngoài bằng `fail_next_with`.
    """

    def __init__(self) -> None:
        self.created: list[client.V1Job] = []
        self._jobs: dict[str, client.V1Job] = {}
        self._next_error: ApiException | None = None

    def fail_next_with(self, status: int) -> None:
        self._next_error = ApiException(status=status)

    def set_status(
        self, name: str, *, succeeded: int = 0, failed: int = 0, active: int = 0
    ) -> None:
        job = self._jobs.get(name)
        assert job is not None, f"chưa có Job tên {name!r} để gán trạng thái"
        job.status = client.V1JobStatus(active=active, succeeded=succeeded, failed=failed)

    def create_namespaced_job(self, namespace: str, body: client.V1Job, **_: object) -> None:
        if self._next_error is not None:
            err, self._next_error = self._next_error, None
            raise err
        name = body.metadata.name
        if name in self._jobs:
            raise ApiException(status=409)
        body.status = client.V1JobStatus()
        self._jobs[name] = body
        self.created.append(body)

    def read_namespaced_job(self, name: str, namespace: str, **_: object) -> client.V1Job:
        if self._next_error is not None:
            err, self._next_error = self._next_error, None
            raise err
        job = self._jobs.get(name)
        if job is None:
            raise ApiException(status=404)
        return job


@pytest.fixture
def fake_batch_api(monkeypatch: pytest.MonkeyPatch) -> _FakeBatchV1Api:
    fake = _FakeBatchV1Api()
    # `load_incluster_config` thành công-không-làm-gì: máy chạy test không ở
    # trong pod, và ta không muốn `JobLauncher.__init__` rơi tiếp xuống
    # `load_kube_config()` rồi đi tìm một kubeconfig có thể không tồn tại.
    monkeypatch.setattr(jobs.config, "load_incluster_config", lambda: None)
    monkeypatch.setattr(jobs.client, "BatchV1Api", lambda: fake)
    return fake


def _launcher() -> JobLauncher:
    return JobLauncher(
        namespace="loom", image="loom-task:test", api_base_url="http://loom-api:8000"
    )


def test_submitting_the_same_run_id_twice_creates_exactly_one_job(
    fake_batch_api: _FakeBatchV1Api,
) -> None:
    """Tên Job tất định (`ingest-{run_id}`) là một quyết định về ĐỘ BỀN: nó làm
    việc submit lại VÔ HẠI, nên một `loom-api` khởi động lại giữa chừng và gọi
    `launch()` lần nữa cho cùng run không bao giờ sinh ra pod THỨ HAI cho cùng
    một run — tức là không bao giờ ghi đôi dữ liệu. 409 từ k8s (Job trùng tên
    đã có) phải bị NUỐT ở đây, không nổi lên thành lỗi cho người gọi: đó chính
    là câu trả lời ĐÚNG cho "Job này đã có rồi".
    """
    launcher = _launcher()

    launcher.launch(RUN_ID, "src-secret", ("loom-internal", "shared"), cpu="50m", memory="512Mi")
    launcher.launch(RUN_ID, "src-secret", ("loom-internal", "shared"), cpu="50m", memory="512Mi")

    assert len(fake_batch_api.created) == 1
    assert fake_batch_api.created[0].metadata.name == job_name(RUN_ID)


def test_a_non_409_error_is_not_swallowed(fake_batch_api: _FakeBatchV1Api) -> None:
    """Nuốt 409 là đúng; nuốt 403 (hay bất kỳ mã nào khác) sẽ giấu mất một Role
    thiếu quyền phía sau một lần "gọi thành công" giả — và triệu chứng lộ ra sẽ
    là run kẹt mãi ở `pending`, rất xa nguyên nhân thật (Role RBAC thiếu
    `create` trên `jobs`). `launch()` chỉ được phép câm nín đúng một mã: mọi mã
    khác phải nổi lên nguyên vẹn để người vận hành thấy đúng lỗi thật.
    """
    launcher = _launcher()
    fake_batch_api.fail_next_with(403)

    with pytest.raises(ApiException) as exc_info:
        launcher.launch(
            RUN_ID, "src-secret", ("loom-internal", "shared"), cpu="50m", memory="512Mi"
        )

    assert exc_info.value.status == 403


def test_status_of_a_missing_job_is_not_an_error(fake_batch_api: _FakeBatchV1Api) -> None:
    """Một run chưa từng phóng Job (hoặc Job đã bị TTL dọn) phải cho
    `exists=False`, KHÔNG được ném ngoại lệ — người gọi (Task 9) cần phân biệt
    được "chưa có Job" với "k8s API lỗi thật" mà không phải bắt riêng 404 ở
    từng nơi gọi."""
    result = _launcher().status(uuid.uuid4())

    assert result == JobStatus(exists=False)


def test_status_reads_integer_counters_not_conditions(fake_batch_api: _FakeBatchV1Api) -> None:
    """Trên k3s 1.32.13 (bản cụm này chạy), một Job Complete mang HAI condition
    cùng `status: "True"` (`SuccessCriteriaMet` VÀ `Complete`) — so khớp chuỗi
    trên `.status.conditions` vỡ ngay trong tình huống đó (xem chú thích ở
    target `probe-single-commit` trong Makefile). Double này CHỦ Ý không có
    trường `conditions` nào cả — nếu `status()` đọc `.status.conditions` thay
    vì ba số đếm nguyên, bài test này ném `AttributeError` chứ không chỉ sai
    giá trị.
    """
    launcher = _launcher()
    launcher.launch(RUN_ID, "src-secret", ("loom-internal", "shared"), cpu="50m", memory="512Mi")
    fake_batch_api.set_status(job_name(RUN_ID), succeeded=1)

    assert launcher.status(RUN_ID) == JobStatus(exists=True, active=0, succeeded=1, failed=0)


def test_the_job_spec_carries_backoff_limit_zero(fake_batch_api: _FakeBatchV1Api) -> None:
    """Không có `backoff_limit=0`, Kubernetes tự thử lại một Job hỏng (mặc định
    6 lần) — Task 1 đã ăn đúng bẫy này và để lại nhiều warehouse Lakekeeper mồ
    côi từ MỘT lần nạp hỏng. Nạp lại ở Giai đoạn 3a phải là một hành động chủ
    động của người dùng, không phải một quyết định lặng lẽ của Kubernetes.
    """
    launcher = _launcher()
    launcher.launch(RUN_ID, "src-secret", ("loom-internal", "shared"), cpu="50m", memory="512Mi")

    (job,) = fake_batch_api.created
    assert job.spec.backoff_limit == 0


def test_the_source_secret_is_passed_by_name_only(fake_batch_api: _FakeBatchV1Api) -> None:
    """`loom-api` chuyển TÊN của Secret nguồn qua `envFrom`, không bao giờ đọc
    giá trị bên trong — đây là lời hứa của `ConnectionDefinition.secret_ref`
    (xem `SECRET_REF_RE` ở `loom_core.item_definitions`) áp dụng ở điểm cuối:
    một lỗi SQL injection trong `loom-api` không có mật khẩu nguồn nào để rò rỉ,
    vì `launch()` chưa từng nhận nó làm tham số.

    Kiểm ba điều, không chỉ một: (1) `envFrom` mang đúng và chỉ đúng tên secret
    nguồn; (2) biến môi trường bí mật chia sẻ (`LOOM_TASK_SHARED_SECRET`) có
    `value=None` — giá trị của nó CHỈ tới qua `secretKeyRef`, không bao giờ là
    một chuỗi literal trong spec; (3) không biến môi trường literal nào khác
    của container mang tên secret nguồn làm giá trị — tức secret nguồn không
    bị rò qua một con đường phụ nào ngoài `envFrom`.
    """
    launcher = _launcher()
    launcher.launch(
        RUN_ID, "prod-pg-credentials", ("loom-internal", "shared-key"), cpu="50m", memory="512Mi"
    )

    (job,) = fake_batch_api.created
    container = job.spec.template.spec.containers[0]

    assert [ef.secret_ref.name for ef in container.env_from] == ["prod-pg-credentials"]

    env_by_name = {e.name: e for e in container.env}
    shared_secret_env = env_by_name["LOOM_TASK_SHARED_SECRET"]
    assert shared_secret_env.value is None
    assert shared_secret_env.value_from.secret_key_ref.name == "loom-internal"
    assert shared_secret_env.value_from.secret_key_ref.key == "shared-key"

    literal_values = [e.value for e in container.env if e.value is not None]
    assert "prod-pg-credentials" not in literal_values
