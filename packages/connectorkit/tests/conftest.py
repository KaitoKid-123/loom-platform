"""Fixture Postgres NGUỒN dùng chung giữa bộ hợp đồng
(`test_connector_contract.py`, đứng ở thư mục này) và bộ test riêng của
`PostgresConnector` (`tests/integration/`).

Đặt Ở ĐÂY — thư mục CHA của `integration/` — chứ không trong
`tests/integration/conftest.py`: pytest nối conftest theo cây thư mục CHỈ
THEO CHIỀU XUỐNG (một file kế thừa fixture của các conftest.py ở thư mục cha
và tổ tiên, không phải của thư mục con). `source_dsn` cần hiện ra ở CẢ
`test_connector_contract.py` (đứng ở `tests/`) LẪN `tests/integration/`
(đứng bên trong `tests/`) — chỉ gốc chung `tests/` mới thấy được cả hai.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from testcontainers.community.postgres import PostgresContainer

# KHÔNG nằm trong deploy/versions.env: Postgres ở đây là CSDL NGUỒN bị
# ingest (một hệ thống ngoài Loom, mô phỏng bằng testcontainer), không phải
# Postgres của cụm — cùng lý do `services/api/tests/integration/pg_support.py`
# và `packages/icebergkit/tests/integration/conftest.py` không pin nó ở đó.
# Cùng tag "17-alpine" với cả hai chỗ trên để mọi Postgres trong bộ test của
# repo này là MỘT bản, không phải ba bản trôi dần theo thời gian.
POSTGRES_IMAGE = "postgres:17-alpine"


@pytest.fixture(scope="session")
def _source_postgres() -> Iterator[PostgresContainer]:
    with PostgresContainer(image=POSTGRES_IMAGE) as container:
        yield container


@pytest.fixture(scope="session")
def source_dsn(_source_postgres: PostgresContainer) -> str:
    """DSN psycopg (`postgresql://...`) tới container Postgres NGUỒN.

    Scope session: MỘT container cho toàn bộ package — cả bộ hợp đồng lẫn bộ
    test riêng của `PostgresConnector` tự tạo/xoá schema riêng của mình BÊN
    TRONG container này (xem `PostgresConnector.__init__` tham số `schema`),
    thay vì mỗi bộ đòi một container mới. Khởi động container mất vài giây;
    không có lý do gì để trả giá đó nhiều lần khi các bảng test ở đây không
    cần cách ly ở tầng container, chỉ cần cách ly ở tầng schema.
    """
    pg = _source_postgres
    return (
        f"postgresql://{pg.username}:{pg.password}"
        f"@{pg.get_container_host_ip()}:{pg.get_exposed_port(5432)}/{pg.dbname}"
    )
