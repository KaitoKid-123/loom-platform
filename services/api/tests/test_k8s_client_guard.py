"""Canh món nợ QUYỀN K8S — `loom-api` giờ tạo được workload trên cụm.

Giai đoạn 1 xây `loom-api` như một control plane chỉ đọc Postgres. Giai đoạn 2b
đã phá nguyên tắc đó một lần (credential gốc MinIO ở `warehouse_provisioning.py`,
xem `test_root_credential_guard.py`). Giai đoạn 3a phá nó lần THỨ HAI: `loom-api`
giờ tạo được `Job` trên cụm — quyền GHI lên Kubernetes, không chỉ đọc Postgres.
Chủ dự án chấp nhận với ĐÚNG điều kiện đã áp cho món nợ thứ nhất: phạm vi phải
hẹp nhất có thể (ĐÚNG MỘT module, `loom_api.jobs`) VÀ phải CANH ĐƯỢC, không chỉ
là một đoạn văn trong tài liệu.

Đọc **AST** của từng module bằng `ast.parse`, KHÔNG import chúng — cùng khuôn
`test_root_credential_guard.py`. Import kéo theo toàn bộ cây phụ thuộc
(`loom_api.main` import mọi router, mọi router import `loom_api.deps`, …), nên
soi `sys.modules` sau khi import sẽ không nói lên module NÀO trong mã nguồn
thật sự viết ra `import kubernetes` — chỉ cây cú pháp mới thấy đúng điều đó.

Phép canh có HAI khẳng định, và khẳng định thứ hai là cái người ta hay quên:

1. Chỉ `jobs.py` được phép `import kubernetes` (dưới mọi hình dạng: `import
   kubernetes`, `import kubernetes.client`, `from kubernetes import ...`).
2. `jobs.py` PHẢI thật sự import nó. Nếu `jobs.py` ngừng chạm k8s, khẳng định
   (1) xanh trong khi canh một đặc quyền không ai dùng — và Role hẹp mà Task 8
   cấp cho `loom-api` trở thành quyền đứng khống cho một thứ không tồn tại.
   Lúc đó việc đúng là XOÁ đặc quyền đó và Role tương ứng trong
   `deploy/helm/loom/templates/api-rbac.yaml`, không phải để phép canh này
   xanh một cách vô nghĩa.
"""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "loom_api"

# Import GỐC bị canh. So khớp trên phần gốc của module path (`a.name.split(".")[0]`
# cho `import kubernetes.client`, `node.module.split(".")[0]` cho `from
# kubernetes.client import Foo`) để mọi hình dạng import đều bị bắt, không chỉ
# `import kubernetes` trần.
GUARDED_IMPORTS = {"kubernetes"}

# Module DUY NHẤT được phép tham chiếu GUARDED_IMPORTS.
ALLOWED_MODULE = "jobs.py"


def _modules() -> list[Path]:
    found = sorted(SRC.rglob("*.py"))
    assert found, f"không thấy module nào trong {SRC} — phép kiểm này sẽ xanh oan"
    return found


def _k8s_imports(path: Path) -> list[tuple[str, int]]:
    """(tên gốc, số dòng) cho mọi `import`/`from ... import ...` khớp GUARDED_IMPORTS.

    `node.level == 0` loại import tương đối (`from . import x`) — một import
    tương đối không bao giờ trỏ ra một package bên thứ ba như `kubernetes`, nên
    không cần xét.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend(
                (root, node.lineno)
                for alias in node.names
                if (root := alias.name.split(".")[0]) in GUARDED_IMPORTS
            )
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            root = node.module.split(".")[0]
            if root in GUARDED_IMPORTS:
                out.append((root, node.lineno))
    return out


@pytest.mark.parametrize("module", _modules(), ids=lambda p: p.name)
def test_only_one_module_talks_to_kubernetes(module: Path) -> None:
    if module.name == ALLOWED_MODULE:
        return
    found = _k8s_imports(module)
    assert not found, "\n".join(
        f"{module.name}:{line} import `{name}` — chỉ {ALLOWED_MODULE} được phép "
        "chạm Kubernetes API (xem docstring đầu file)"
        for name, line in found
    )


def test_the_allowed_module_still_exists_and_actually_uses_it() -> None:
    """Canh cho chính phép canh: nếu `jobs.py` bị đổi tên hay không còn import
    `kubernetes` nữa, phép kiểm trên xanh OAN — không phải vì món nợ đã trả, mà
    vì `ALLOWED_MODULE` giờ trỏ vào một chỗ chết, hoặc quyền k8s đã cấp không
    còn ai dùng.

    Nếu rơi vào trường hợp sau — `jobs.py` không còn chạm k8s — việc đúng là
    XOÁ luôn đặc quyền (settings, dependency `kubernetes`, Role trong
    `deploy/helm/loom/templates/api-rbac.yaml`), không phải để phép canh này
    tiếp tục xanh trong khi bảo vệ một thứ không ai dùng.
    """
    path = SRC / ALLOWED_MODULE
    assert path.exists(), f"{ALLOWED_MODULE} không còn tồn tại — sửa ALLOWED_MODULE?"
    assert _k8s_imports(path), (
        f"{ALLOWED_MODULE} không còn import `kubernetes` — nếu loom-api không còn "
        "cần tạo Job, xoá luôn Role k8s ở deploy/helm/loom/templates/api-rbac.yaml "
        "và dependency `kubernetes` khỏi pyproject.toml, đừng để phép canh này "
        "canh một cái vỏ rỗng"
    )


def test_the_guard_can_see_a_violation(tmp_path: Path) -> None:
    """Phép canh cho chính phép canh, phần hai: `_k8s_imports` phải thấy đúng
    MỌI hình dạng import thật — `import kubernetes`, `import kubernetes.client`,
    và `from kubernetes.client import BatchV1Api` — không chỉ dạng trần nhất."""
    source = (
        "import kubernetes\nimport kubernetes.client\nfrom kubernetes.client import BatchV1Api\n"
    )
    probe = tmp_path / "probe.py"
    probe.write_text(source, encoding="utf-8")
    assert _k8s_imports(probe) == [
        ("kubernetes", 1),
        ("kubernetes", 2),
        ("kubernetes", 3),
    ]
