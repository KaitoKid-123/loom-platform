"""Cấp phát (hoặc không) một warehouse Lakekeeper khi tạo item.

Khoảng trống Giai đoạn 2a phát hiện: tạo một item `type='lakehouse'` qua API
trước đây chỉ chèn một hàng Postgres. Không có warehouse Lakekeeper nào được
tạo, nên `GET /catalog/v1/config` trả 400 (thiếu tham số `warehouse`) và
`loom-query` hỏng ngay khi có ai mở nó — một lỗi CHỈ lộ ra lúc dùng, không lộ
ra lúc tạo.

**Đây là module DUY NHẤT trong `loom_api` được đọc
`Settings.storage_root_access_key`/`storage_root_secret_key`.** Giai đoạn 1
xây `loom-api` như một control plane KHÔNG đọc secret nào — `connection` chỉ
giữ `secret_ref`, không giữ mật khẩu (xem `SECRET_REF_RE` ở
`loom_core.item_definitions`). Cấp một warehouse S3 mới đòi credential GỐC
của MinIO — thứ mở được MỌI prefix của MỌI workspace, không riêng một
workspace nào — nên đây là một MÓN NỢ, được chủ dự án chấp nhận với điều
kiện: phạm vi đọc credential đó phải hẹp nhất có thể VÀ phải CANH ĐƯỢC, không
chỉ là một đoạn văn trong tài liệu. `services/api/tests/
test_root_credential_guard.py` đọc AST của mọi module trong `loom_api` và
khẳng định đúng MỘT module (module này) tham chiếu hai trường đó — thêm một
chỗ đọc thứ hai (kể cả một import gián tiếp gán field vào biến rồi truyền đi)
làm phép canh đó ĐỎ, nêu đúng tên module vi phạm.

**Vì sao warehouse phải tồn tại TRƯỚC khi item được commit vào Postgres:**
Lakekeeper và Postgres là hai kho khác nhau, không chia sẻ một transaction.
Nếu item commit trước và việc tạo warehouse sau đó thất bại, ta có một item
đang SỐNG trỏ vào một lakehouse RỖNG — lỗi đó im lặng cho tới khi người dùng
mở nó ra. Ngược lại, warehouse tạo xong nhưng item không tạo được (tên trùng,
định nghĩa sai...) để lại một warehouse MỒ CÔI: tốn chỗ, nhưng ồn ào theo
đúng nghĩa tốt — nó không lừa ai rằng có một lakehouse dùng được. Đổi một lỗi
im lặng lấy một lỗi rác là đúng hướng; dọn warehouse mồ côi là nợ đã ghi ở
README. `loom_api.item_store.ItemStore.create` gọi `provision_warehouse` (qua
callback `provision`) SAU khi validate + kiểm quyền nhưng TRƯỚC khi tạo bất kỳ
đối tượng `Item`/`ItemVersion` nào — xem docstring ở đó.

**Xoá mềm một lakehouse KHÔNG xoá warehouse của nó** — cố ý, và module này
không có hàm xoá nào. Hai lý do: (1) lịch sử version vẫn cần warehouse sống để
`restore_version` dùng lại được; (2) Lakekeeper từ chối xoá một warehouse còn
bảng bên trong bằng `409 WarehouseNotEmpty`, và `force=true` KHÔNG vượt qua
được điều đó (đã kiểm ở Giai đoạn 2a) — tự ý xoá ở đây chỉ đổi một thao tác
xoá mềm gọn gàng thành một thao tác có thể nổ 409 giữa chừng.
"""

import uuid

from loom_core.config import Settings
from loom_core.item_definitions import ItemType
from loom_iceberg.warehouse import create_warehouse, ensure_bootstrapped
from loom_storage.credentials import prefix_for_lakehouse


def provision_warehouse(
    settings: Settings,
    item_id: uuid.UUID,
    *,
    item_type: ItemType,
    workspace_id: uuid.UUID,
) -> None:
    """No-op cho mọi `ItemType` TRỪ `lakehouse` — `connection`/`pipeline`/
    `sql_script` không có dữ liệu Iceberg nào để cấp chỗ, và gọi Lakekeeper
    cho chúng chỉ là một round trip mạng lãng phí (hoặc tệ hơn, một warehouse
    không ai dùng).

    `item_id` PHẢI là id sẽ trở thành `Item.id` thật trong Postgres —
    warehouse đặt tên theo ĐÚNG chuỗi `str(item_id)`, khớp quy ước mà
    `loom_query.runner` giả định khi mở catalog Iceberg
    (`warehouse=str(lakehouse_id)`, xem docstring của nó). Đặt theo tên item
    thay vì id sẽ vỡ ngay khi ai đó đổi tên lakehouse — tên đổi được, id thì
    không, và Lakekeeper không có API "rename warehouse".

    `key-prefix` dùng `prefix_for_lakehouse` — KHÔNG tự ghép chuỗi: đó là ranh
    giới mà policy STS của `MinioStsProvider` giới hạn vào (xem docstring của
    `prefix_for_lakehouse`), và một prefix trượt ra ngoài quy ước đó là một lỗ
    hổng, không phải một tiểu tiết định dạng.
    """
    if item_type is not ItemType.lakehouse:
        return
    ensure_bootstrapped(settings.lakekeeper_url)
    create_warehouse(
        settings.lakekeeper_url,
        name=str(item_id),
        bucket=settings.storage_bucket,
        key_prefix=prefix_for_lakehouse(workspace_id, item_id),
        s3_endpoint=settings.storage_endpoint,
        access_key=settings.storage_root_access_key,
        secret_key=settings.storage_root_secret_key,
    )
