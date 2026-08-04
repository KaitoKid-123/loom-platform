"""`Principal` là danh tính mà RBAC nhìn thấy. Hai bất biến của nó không phải
chuyện thẩm mỹ:

- `groups` phải ổn định giữa hai lần dựng. Task 11 dùng nó làm phần của cache key
  cho quyền trong phạm vi request; một thứ tự phụ thuộc IdP làm cache đó vô dụng
  (miss mọi lần) hoặc tệ hơn là không nhất quán.
- tên nhóm rỗng và subject rỗng phải bị chặn ở BIÊN. `principal_group = ''` trong
  role_assignment khớp với một nhóm tên rỗng; validator là thứ chặn một cái tên
  như thế đi vào từ token hoặc từ một hàng DB hỏng.
"""

import uuid

import pytest
from pydantic import ValidationError

from loom_core.schemas import Principal

# Tám tên, đưa vào theo thứ tự GIẢM DẦN. Không phải trang trí: nếu ai bỏ
# `sorted()` và để lại `set()`, thứ tự đi ra là thứ tự băm — trùng đúng thứ tự
# tăng dần với xác suất 1/8! ≈ 0,0025%. Với hai phần tử thì xác suất là 50%, tức
# là một mutation bỏ `sorted()` sẽ sống sót một nửa số lần chạy.
DESCENDING = ["zulu", "yankee", "xray", "whiskey", "victor", "uniform", "tango", "sierra"]


def make(**overrides: object) -> Principal:
    kwargs: dict[str, object] = {
        "user_id": uuid.uuid4(),
        "subject": "CgRsb25n",
        "email": "long@loom.local",
        "display_name": "Long",
    }
    kwargs.update(overrides)
    return Principal(**kwargs)  # type: ignore[arg-type]


def test_principal_group_names_are_deduplicated() -> None:
    """IdP có thể phát nhóm trùng (ví dụ thành viên trực tiếp cộng thành viên
    lồng nhau cùng ánh xạ về một tên)."""
    assert make(groups=["data-eng", "admins", "data-eng"]).groups == ("admins", "data-eng")


def test_principal_group_names_are_sorted() -> None:
    """Đưa vào theo thứ tự giảm dần; phải đi ra tăng dần. Xem ghi chú ở
    DESCENDING về lý do tám tên chứ không phải hai."""
    assert make(groups=list(DESCENDING)).groups == tuple(sorted(DESCENDING))


def test_principal_groups_are_a_tuple_so_the_identity_is_hashable() -> None:
    """Cache key trong phạm vi request cần băm được — list thì không."""
    p = make(groups=["admins"])
    assert isinstance(p.groups, tuple)
    assert hash(p) == hash(make(user_id=p.user_id, groups=["admins"]))


def test_principal_defaults_to_no_groups() -> None:
    assert make().groups == ()


def test_principal_treats_missing_groups_claim_as_no_groups() -> None:
    assert make(groups=None).groups == ()


def test_principal_rejects_empty_group_name() -> None:
    """Nhóm tên rỗng khớp với mọi hàng có principal_group = '' nếu ai đó chèn
    được — chặn ở biên thay vì tin IdP."""
    with pytest.raises(ValidationError):
        make(groups=["ok", ""])


def test_principal_rejects_whitespace_only_group_name() -> None:
    """`" "` sau khi strip là rỗng. Nếu chỉ kiểm `not n` TRƯỚC khi strip thì tên
    này đi qua và lưu vào DB dưới dạng khác rỗng nhưng vô nghĩa."""
    with pytest.raises(ValidationError):
        make(groups=["ok", "   "])


def test_principal_strips_surrounding_whitespace_from_group_names() -> None:
    """Nếu không strip thì `"admins"` và `"admins "` là hai nhóm khác nhau với
    RBAC, và người quản trị không nhìn thấy sự khác biệt."""
    assert make(groups=[" admins ", "admins"]).groups == ("admins",)


def test_principal_rejects_a_bare_string_for_groups() -> None:
    """Một chuỗi cũng iterate được — `groups="admins"` sẽ lặng lẽ thành tám nhóm
    một-ký-tự. Đây là hình dạng mà một claim `groups` sai kiểu thật sự có."""
    with pytest.raises(ValidationError):
        make(groups="admins")


def test_principal_rejects_empty_subject() -> None:
    """IdTokenClaims.__post_init__ chặn subject rỗng vì load_session() dựng lại
    danh tính THẲNG từ hàng DB, đi vòng qua verify(). Từ Task 3 load_session()
    trả Principal, nên bất biến phải đi theo sang đây — nếu không, một hàng
    app_user hỏng lại dựng ra được một danh tính hợp lệ."""
    with pytest.raises(ValidationError):
        make(subject="")


def test_principal_rejects_whitespace_only_subject() -> None:
    with pytest.raises(ValidationError):
        make(subject="   ")


def test_principal_is_frozen() -> None:
    """Một danh tính bị sửa sau khi xác thực là một lỗ leo thang quyền."""
    p = make(groups=["admins"])
    with pytest.raises(ValidationError):
        p.groups = ("root",)  # type: ignore[misc]
