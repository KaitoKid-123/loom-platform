"""`secret_ref` -> TÊN k8s Secret, và những dạng phải hỏng NGAY.

Tách khỏi phần HTTP (`tests/integration/test_ingest_api.py`) vì phần này thuần
hàm: không database, không Docker. Nhờ vậy nó chạy trong `make test`, và một
dạng `secret_ref` không dùng được lộ ra trong vài mili giây thay vì sau khi
container Postgres đã khởi động xong.

Điều đang được canh không phải "regex có khớp không" mà là: `loom-api` chỉ bao
giờ moi ra được một cái TÊN từ ô này. Không có đường nào trong file này (hay
trong `ingest_service.py`) đọc được giá trị nằm sau cái tên đó.
"""

import pytest

from loom_api.ingest_service import SecretRefUnusable, resolve_secret_ref, secret_name_for


def test_a_k8s_ref_splits_into_namespace_name_and_key() -> None:
    location = resolve_secret_ref("k8s://loom/source-pg#password")

    assert (location.namespace, location.name, location.key) == ("loom", "source-pg", "password")


def test_a_vault_ref_says_why_it_cannot_work_here() -> None:
    """`vault://` HỢP LỆ với `SECRET_REF_RE` (Giai đoạn 1), nên nó tới được tận
    đây — nhưng cụm local không với tới Vault. Thông báo phải nói đúng lý do đó
    chứ không phải "sai định dạng": người dùng vừa nhập một giá trị mà API đã
    nhận ở chỗ khác (lúc tạo item `connection`), và "sai định dạng" sẽ đẩy họ đi
    sửa dấu gạch chéo trong một chuỗi vốn không sai.

    Khẳng định trên `k8s://` chứ không chỉ trên `"vault"`: thông báo "không dùng
    được" chung chung có nhúng cả `secret_ref` vào, nên nó CŨNG chứa chữ
    "vault" — một phép kiểm chỉ tìm chữ đó sẽ xanh y nguyên sau khi nhánh giải
    thích bị xoá, tức là canh một thứ nó không thấy. Dạng thay thế phải có mặt
    thì thông báo mới nói được cho người dùng phải làm gì.
    """
    with pytest.raises(SecretRefUnusable) as exc_info:
        resolve_secret_ref("vault://loom/source-pg#password")

    message = str(exc_info.value)
    assert "Vault" in message
    assert "k8s://" in message


def test_a_password_pasted_into_the_field_is_not_a_ref() -> None:
    with pytest.raises(SecretRefUnusable):
        resolve_secret_ref("hunter2")


def test_a_trailing_newline_does_not_sneak_through() -> None:
    r"""`\Z` chứ không `$` — cùng cái bẫy `SECRET_REF_RE` đã ghi ở
    `loom_core.item_definitions`: trong Python `$` khớp cả ngay trước một `\n`
    cuối chuỗi, nên `^...$` nhận một chuỗi mà mắt người đọc là hai dòng. Ở đây
    hậu quả cụ thể hơn: tên Secret mang `\n` đi thẳng vào `metadata` của Job.
    """
    with pytest.raises(SecretRefUnusable):
        resolve_secret_ref("k8s://loom/source-pg#password\n")


def test_the_launcher_only_ever_learns_the_name() -> None:
    assert secret_name_for("k8s://loom/source-pg#password", "loom") == "source-pg"


def test_a_secret_in_another_namespace_is_refused() -> None:
    """`envFrom` chỉ chiếu được Secret nằm CÙNG namespace với Job, và Job luôn
    được tạo trong `settings.task_namespace` (xem `JobLauncher.__init__`). Một
    ref trỏ sang namespace khác vì vậy không phải là "gần đúng": nó tạo ra một
    Job hỏi xin một Secret CÙNG TÊN trong namespace của Job — thứ thường không
    tồn tại. Pod kẹt ở `CreateContainerConfigError`, run kẹt ở `pending`, và
    triệu chứng hiện ra xa hẳn nguyên nhân. Từ chối ngay, ở đây, với một câu
    nói đúng namespace nào sai.
    """
    with pytest.raises(SecretRefUnusable) as exc_info:
        secret_name_for("k8s://khac/source-pg#password", "loom")

    assert "khac" in str(exc_info.value)
