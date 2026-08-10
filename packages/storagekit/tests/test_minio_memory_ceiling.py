"""Phép canh trần bộ nhớ của MinIO trong `deploy/infra/minio.yaml`.

Giai đoạn 2a đặt limit 320Mi từ một phép đo lúc cụm NGHỈ (~250 MiB) cộng biên.
Phép đo 50 GB của Giai đoạn 2c chứng minh con số đó sai dưới tải ghi thật: MinIO
bị OOMKilled ở lô 40/200, và bài đo chết theo với `curlCode 7`.

Cơ chế, vì nó quyết định phép canh này canh cái gì: MinIO là Go, và Go KHÔNG đọc
hạn mức cgroup. Bộ thu gom rác nhắm theo GOGC — theo tốc độ phình của heap — nên
nó không hề biết có một cái trần ở trên. Nó phình qua trần và hạt nhân giết tiến
trình. Nâng limit KHÔNG tự nó sửa được điều đó: nâng lên bao nhiêu thì GC vẫn mù,
chỉ là chết muộn hơn. Thứ sửa được là `GOMEMLIMIT`, giới hạn MỀM mà GC có nhìn.

Nên tính chất phải giữ không phải "limit đủ to" mà là: **có GOMEMLIMIT, và nó
nằm dưới limit cứng với một khoảng dư thật.** Hạ limit về 320Mi mà quên
GOMEMLIMIT=352MiB sẽ làm giới hạn mềm nằm TRÊN giới hạn cứng — vô nghĩa, và đúng
là cách hồi quy về lỗi cũ. Phép canh này đỏ đúng lúc đó.
"""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

MANIFEST = Path(__file__).resolve().parents[3] / "deploy" / "infra" / "minio.yaml"

# Khoảng dư tối thiểu giữa giới hạn mềm và giới hạn cứng. GOMEMLIMIT chỉ tính
# phần bộ nhớ do runtime Go quản — stack của thread hệ điều hành, vùng mmap và
# buffer cgo nằm NGOÀI nó nhưng vẫn tính vào cgroup. Không chừa khoảng này thì GC
# có thể đang tuân thủ hoàn hảo mà tiến trình vẫn bị giết.
MIN_HEADROOM_MIB = 64

# Heap (`anon`) đo thật được trong lúc ghi, xem khối giải thích trong minio.yaml.
# Limit cứng phải trên nó, nếu không thì không lượng GC nào cứu được.
MEASURED_ANON_MIB = 271


def _quantity_to_mib(value: str) -> int:
    """Đổi một lượng bộ nhớ sang MiB.

    Kubernetes và Go dùng HAI cách viết khác nhau cho cùng một thứ, và đó chính
    là cái bẫy: k8s viết `448Mi`, Go viết `352MiB`. So sánh chúng dưới dạng chuỗi
    thì luôn sai.
    """
    match = re.fullmatch(r"(\d+)(Ki|Mi|Gi|KiB|MiB|GiB|K|M|G)?B?", value.strip())
    if match is None:
        raise ValueError(f"không đọc được lượng bộ nhớ: {value!r}")
    number, unit = int(match.group(1)), (match.group(2) or "").rstrip("B")
    factor = {
        "": 1 / 1024**2,
        "Ki": 1 / 1024,
        "Mi": 1,
        "Gi": 1024,
        "K": 1000 / 1024**2,
        "M": 1000**2 / 1024**2,
        "G": 1000**3 / 1024**2,
    }[unit]
    return int(number * factor)


@pytest.fixture(scope="module")
def minio_container() -> dict[str, Any]:
    docs = [d for d in yaml.safe_load_all(MANIFEST.read_text()) if d]
    deployments = [d for d in docs if d.get("kind") == "Deployment"]
    assert len(deployments) == 1, "mong đúng một Deployment trong minio.yaml"
    containers = deployments[0]["spec"]["template"]["spec"]["containers"]
    named = [c for c in containers if c["name"] == "minio"]
    assert len(named) == 1, "mong đúng một container tên 'minio'"
    return named[0]


def _env(container: dict[str, Any], name: str) -> str | None:
    for entry in container.get("env", []):
        if entry["name"] == name:
            return entry.get("value")
    return None


def test_gomemlimit_is_declared(minio_container: dict[str, Any]) -> None:
    """Không có nó thì GC của Go mù với cgroup — đo thật: chết ở lô 40/200."""
    assert _env(minio_container, "GOMEMLIMIT") is not None, (
        "MinIO mất GOMEMLIMIT. Go không đọc hạn mức cgroup, nên thiếu biến này "
        "thì bộ thu gom rác không biết có trần và tiến trình bị OOMKill dưới tải "
        "ghi thật — không phải giả thuyết, xem phép đo 50 GB ở Giai đoạn 2c."
    )


def test_soft_limit_sits_below_the_hard_limit_with_headroom(
    minio_container: dict[str, Any],
) -> None:
    """Giới hạn mềm PHẢI dưới giới hạn cứng, và dưới một khoảng thật.

    Đây là phép canh bắt được đúng kiểu hồi quy nguy hiểm nhất: ai đó hạ
    `limits.memory` về lại 320Mi để tiết kiệm ngân sách RAM mà không đụng tới
    GOMEMLIMIT=352MiB. Lúc đó giới hạn mềm nằm TRÊN giới hạn cứng, GC không bao
    giờ bị thúc, và MinIO chết y như trước — trong khi manifest trông vẫn "có
    GOMEMLIMIT" nên phép canh ở trên vẫn xanh.
    """
    declared = _env(minio_container, "GOMEMLIMIT")
    # Không có dòng này, xoá GOMEMLIMIT làm phép kiểm đỏ bằng một ValueError từ
    # bộ đọc đơn vị — vẫn đỏ, nhưng thông báo nói về cú pháp chứ không nói về
    # thứ thật sự hỏng. Một phép canh đỏ sai lý do là một phép canh khó tin.
    assert declared is not None, "không có GOMEMLIMIT để so với limit"

    soft = _quantity_to_mib(declared)
    hard = _quantity_to_mib(minio_container["resources"]["limits"]["memory"])

    assert soft < hard, (
        f"GOMEMLIMIT {soft} MiB không dưới limit {hard} MiB — giới hạn mềm nằm "
        f"trên giới hạn cứng thì nó không giới hạn gì cả."
    )
    assert hard - soft >= MIN_HEADROOM_MIB, (
        f"chỉ còn {hard - soft} MiB giữa GOMEMLIMIT và limit, cần ít nhất "
        f"{MIN_HEADROOM_MIB} MiB. GOMEMLIMIT không tính stack của thread hệ điều "
        f"hành, vùng mmap và buffer cgo, nhưng cgroup thì có tính."
    )


def test_hard_limit_clears_the_measured_working_set(
    minio_container: dict[str, Any],
) -> None:
    """Đo thật trong lúc ghi: heap 271 MiB, page cache 39 MiB.

    Phần heap KHÔNG thu hồi được, nên nó là sàn thật. Một limit dưới nó thì
    GOMEMLIMIT cũng không cứu nổi — GC không thể giải phóng thứ đang được dùng.
    """
    hard = _quantity_to_mib(minio_container["resources"]["limits"]["memory"])
    assert hard > MEASURED_ANON_MIB, (
        f"limit {hard} MiB không vượt được heap {MEASURED_ANON_MIB} MiB đo được "
        f"trong lúc ghi 50 GB."
    )

    # Và giới hạn MỀM cũng phải trên sàn đó. Thiếu khẳng định này thì hạ CẢ HAI
    # con số cùng lúc sẽ lọt: GOMEMLIMIT=256MiB với limit=320Mi giữ đúng quan hệ
    # mềm-dưới-cứng mà ba phép kiểm kia đòi, nhưng nó CHÍNH LÀ trần 320Mi đã đo
    # được là chết ở lô 40/200. Một giới hạn mềm dưới heap sống là một mục tiêu
    # GC không bao giờ với tới — nó chỉ làm bộ thu gom quay cuồng rồi vẫn chết.
    soft = _quantity_to_mib(_env(minio_container, "GOMEMLIMIT") or "")
    assert soft > MEASURED_ANON_MIB, (
        f"GOMEMLIMIT {soft} MiB nằm DƯỚI heap {MEASURED_ANON_MIB} MiB đo được — "
        f"GC không thể giải phóng thứ đang được dùng."
    )


def test_requests_do_not_exceed_limits(minio_container: dict[str, Any]) -> None:
    """Kubelet từ chối pod có request lớn hơn limit. Dễ gây ra khi sửa một trong
    hai con số mà quên con số kia — và lỗi chỉ lộ ra lúc apply, không phải ở CI."""
    resources = minio_container["resources"]
    assert _quantity_to_mib(resources["requests"]["memory"]) <= _quantity_to_mib(
        resources["limits"]["memory"]
    )
