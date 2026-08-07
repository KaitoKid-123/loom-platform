"""Canh món nợ credential gốc MinIO — phép kiểm quan trọng nhất của task này.

`Settings.storage_root_access_key`/`storage_root_secret_key` là credential
GỐC của MinIO: nó mở được MỌI prefix của MỌI workspace, không riêng một
workspace nào (khác mọi credential khác mà `loom-api` từng cầm). Giai đoạn 1
xây `loom-api` như một control plane KHÔNG đọc secret nào — `connection` chỉ
giữ `secret_ref`, không giữ mật khẩu (xem `SECRET_REF_RE` và
`test_every_definition_type_forbids_unknown_fields` ở
`packages/core/tests/test_item_definitions.py`). Cầm credential gốc để tự cấp
phát warehouse Lakekeeper là phá đúng nguyên tắc đó — chủ dự án chấp nhận, với
điều kiện: phạm vi đọc nó phải hẹp nhất có thể (ĐÚNG MỘT module,
`loom_api.warehouse_provisioning`) VÀ phải CANH ĐƯỢC, không chỉ là một đoạn
văn trong tài liệu.

Đọc **AST** của từng module bằng `ast.parse`, KHÔNG import chúng — cùng khuôn
`packages/sqlkit/tests/test_no_io.py`. Import kéo theo toàn bộ cây phụ thuộc
(`loom_api.main` import mọi router, mọi router import `loom_api.deps`, …), nên
soi `sys.modules` sau khi import sẽ không nói lên module NÀO trong mã nguồn
thật sự viết ra chữ `.storage_root_access_key` — ast.walk() trên cây cú pháp
mới thấy đúng điều đó.
"""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "loom_api"

# Hai trường bị canh. KHÔNG bao gồm `storage_endpoint`/`storage_bucket`/
# `lakekeeper_url`: những trường đó không phải secret, không có gì để canh.
GUARDED_FIELDS = {"storage_root_access_key", "storage_root_secret_key"}

# Module DUY NHẤT được phép tham chiếu GUARDED_FIELDS.
ALLOWED_MODULE = "warehouse_provisioning.py"


def _modules() -> list[Path]:
    found = sorted(SRC.rglob("*.py"))
    assert found, f"không thấy module nào trong {SRC} — phép kiểm này sẽ xanh oan"
    return found


def _references(path: Path) -> list[tuple[str, int]]:
    """(tên field, số dòng) cho mọi lần truy cập thuộc tính khớp GUARDED_FIELDS.

    Bắt CẢ `ast.Attribute` (`settings.storage_root_secret_key`) LẪN
    `ast.keyword`/`ast.arg` mang đúng tên đó làm biến cục bộ — không, hai loại
    sau không phải "đọc trường Settings", cố ý bỏ qua để phép kiểm không báo
    động giả với một tham số hàm tình cờ trùng tên. `ast.Attribute` là hình
    dạng DUY NHẤT mà việc đọc một trường Pydantic tạo ra trong mã nguồn.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [
        (node.attr, node.lineno)
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr in GUARDED_FIELDS
    ]


def test_only_one_module_reads_the_root_minio_credential() -> None:
    offenders = {
        module.name: refs
        for module in _modules()
        if module.name != ALLOWED_MODULE and (refs := _references(module))
    }
    assert not offenders, (
        "credential GỐC MinIO bị đọc ngoài "
        + ALLOWED_MODULE
        + ":\n"
        + "\n".join(
            f"  {name}: " + ", ".join(f"{field} (dòng {line})" for field, line in refs)
            for name, refs in offenders.items()
        )
    )


def test_the_allowed_module_still_exists_and_actually_uses_the_field() -> None:
    """Canh cho chính phép canh: nếu `warehouse_provisioning.py` bị đổi tên hay
    không còn đọc field nào nữa, phép kiểm trên xanh OAN — không phải vì nợ đã
    trả, mà vì `ALLOWED_MODULE` giờ trỏ vào một chỗ chết."""
    path = SRC / ALLOWED_MODULE
    assert path.exists(), f"{ALLOWED_MODULE} không còn tồn tại — sửa ALLOWED_MODULE?"
    assert _references(path), (
        f"{ALLOWED_MODULE} không còn đọc credential gốc nào — nếu warehouse "
        "không còn cần credential gốc, xoá field khỏi Settings luôn, đừng để "
        "phép canh này canh một cái vỏ rỗng"
    )


def test_the_guard_can_see_a_violation(tmp_path: Path) -> None:
    """Phép canh cho chính phép canh, phần hai: `_references` phải thấy đúng
    dạng vi phạm THẬT — một hàm đọc `settings.storage_root_secret_key` qua một
    tham số tên khác `settings`, không chỉ literal `settings.<field>`."""
    source = (
        "def leak(cfg):\n    return cfg.storage_root_access_key + cfg.storage_root_secret_key\n"
    )
    probe = tmp_path / "probe.py"
    probe.write_text(source, encoding="utf-8")
    assert _references(probe) == [
        ("storage_root_access_key", 2),
        ("storage_root_secret_key", 2),
    ]


@pytest.mark.parametrize("field", sorted(GUARDED_FIELDS))
def test_field_names_match_settings(field: str) -> None:
    """Nếu ai đó đổi tên field trong `loom_core.config.Settings` mà quên sửa
    `GUARDED_FIELDS`, phép kiểm chính vẫn "xanh" — nhưng nó đang canh một cái
    tên không còn tồn tại. Neo `GUARDED_FIELDS` vào Settings thật để đổi tên
    field mà quên chỗ này thì đỏ NGAY, ở một thông báo nói đúng nguyên nhân."""
    from loom_core.config import Settings

    assert field in Settings.model_fields, (
        f"GUARDED_FIELDS có '{field}' nhưng Settings không còn field đó"
    )
