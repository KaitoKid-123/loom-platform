"""Đọc tên ràng buộc ra khỏi một `IntegrityError`.

Ở chung một chỗ vì hai store cùng cần, và vì nó mã hoá một chi tiết đã phải trả
giá để tìm ra: `exc.orig` là shim DBAPI của SQLAlchemy và KHÔNG có
`constraint_name`. Copy sang store thứ hai mà viết thiếu tầng `__cause__` thì
điều kiện không bao giờ khớp và mọi lỗi ràng buộc thành 500 — im lặng, và chỉ
thấy khi có người dùng thật gặp phải.
"""

from sqlalchemy.exc import IntegrityError


def constraint_of(exc: IntegrityError) -> str | None:
    """Tên ràng buộc đã vỡ, hoặc None nếu không đọc được."""
    cause = getattr(exc.orig, "__cause__", None)
    name = getattr(cause, "constraint_name", None)
    return str(name) if name is not None else None
