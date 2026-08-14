"""Ngân sách connection tới Aiven, canh như một TÍNH CHẤT chứ không phải một quy ước.

Service Aiven của dự án có `max_connections=20`, và nó KHÔNG chỉ phục vụ Loom.
Đo ngày 2026-08-14 trên chính service đó:

    lakekeeper   7 idle       <- pool riêng của Lakekeeper
    bi_portal    5 idle       <- ứng dụng KHÁC của chủ dự án (JDBC), không phải Loom
    loom         3 idle + 1 active
                              <- loom-api

Trước bản này, quyền của hai thành phần Loom cộng lại là **25 trên 20 slot**:
loom-api 5+5, còn Lakekeeper v0.9.2 mặc định đọc 10 + ghi 5. Cụm BỘI CHI ngay từ
thiết kế. Nó không vỡ lúc nghỉ — mỗi pool chỉ phình tới phần nó thật sự cần — mà
vỡ đúng lúc một consumer MỚI xin connection đầu tiên. Consumer mới đó là pod nạp,
và đó là cách `make smoke` trượt 13/14 ở đúng ô `/ingest` với
`SourceUnreachable: ... remaining connection slots are reserved for roles with
the SUPERUSER attribute`.

Phép canh này giữ tổng QUYỀN, không phải tổng ĐANG DÙNG. Quyền là thứ quyết định
liệu một pod nạp có xin được connection ở khoảnh khắc xấu nhất hay không, và nó
là thứ duy nhất đọc được từ cấu hình mà không cần một server thật.

**Giới hạn phải nói ra:** initContainer `migrate` của Lakekeeper KHÔNG bị
`LAKEKEEPER__PG_WRITE_POOL_CONNECTIONS` chặn — nó dựng pool riêng không đặt
`max_connections` (đã kiểm ở mã v0.9.2). Đó là một đỉnh NGẮN lúc khởi động, nằm
ngoài con số dưới đây, và phép canh này không giả vờ bao được nó.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from loom_core.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[3]
VALUES = REPO_ROOT / "deploy" / "helm" / "loom" / "values.yaml"

# Đo được trên chính service, không phải một con số tra từ tài liệu gói.
AIVEN_MAX_CONNECTIONS = 20

# Slot mà Loom KHÔNG được đụng tới, và lý do từng phần:
#   5  `bi_portal` — ứng dụng khác của chủ dự án, đo được đang giữ 5.
#   3  Aiven giữ lại cho vai trò SUPERUSER (thông báo lỗi khi hết slot nói đúng
#      chữ đó). Không đọc được `superuser_reserved_connections` lúc viết phép
#      canh này vì service đã bão hoà — nên 3 là con số DÈ DẶT theo mặc định
#      của PostgreSQL, và nếu đo được số thật thì sửa ở đây.
#   2  pod nạp: một `PostgresConnector` mở một connection cho cursor có tên,
#      cộng một cho phép kiểm schema.
FOREIGN_APP_SLOTS = 5
SUPERUSER_RESERVED = 3
INGEST_POD_SLOTS = 2

LOOM_SERVICE_BUDGET = (
    AIVEN_MAX_CONNECTIONS - FOREIGN_APP_SLOTS - SUPERUSER_RESERVED - INGEST_POD_SLOTS
)


def _lakekeeper_values() -> dict[str, object]:
    return dict(yaml.safe_load(VALUES.read_text())["lakekeeper"])


def test_lakekeeper_pool_is_bounded_at_all() -> None:
    """Không đặt = nhận mặc định 10+5, tức 75% cả server cho một thành phần."""
    values = _lakekeeper_values()
    for key in ("readPoolConnections", "writePoolConnections"):
        assert key in values, (
            f"lakekeeper.{key} không được khai trong values.yaml. Bỏ trống nghĩa là "
            "nhận mặc định của v0.9.2 (đọc 10 + ghi 5 = 15) trên một server 20 slot."
        )


def test_long_lived_pools_leave_room_for_an_ingest_pod() -> None:
    """Tổng QUYỀN của các pool sống lâu phải chừa chỗ cho một consumer MỚI."""
    values = _lakekeeper_values()
    lakekeeper = int(values["readPoolConnections"]) + int(values["writePoolConnections"])  # type: ignore[arg-type]

    settings = Settings()
    api = settings.db_pool_size + settings.db_max_overflow

    total = lakekeeper + api
    assert total <= LOOM_SERVICE_BUDGET, (
        f"các pool sống lâu của Loom được quyền chiếm {total} connection "
        f"(lakekeeper {lakekeeper} + loom-api {api}), vượt ngân sách "
        f"{LOOM_SERVICE_BUDGET} = {AIVEN_MAX_CONNECTIONS} slot trừ "
        f"{FOREIGN_APP_SLOTS} của bi_portal, {SUPERUSER_RESERVED} Aiven giữ lại, "
        f"{INGEST_POD_SLOTS} cho pod nạp.\n"
        "Bội chi không vỡ lúc nghỉ — nó vỡ đúng lúc pod nạp xin connection đầu tiên."
    )
