"""Dựng container Postgres và chạy alembic — dùng chung, không sao chép.

Task 8 đã viết những hàm này bên trong `test_migrations.py`. Task 9 cần đúng
chúng, và một container Postgres thứ hai cho cùng một schema là ba giây initdb
cộng một bản sao của cùng một logic để lệch. Trích ra đây, `conftest.py` dựng
fixture từ chúng, `test_migrations.py` vẫn gọi chúng cho container riêng của
test downgrade.
"""

import os
import subprocess
from pathlib import Path

from testcontainers.community.postgres import PostgresContainer

API_DIR = Path(__file__).resolve().parents[2]

POSTGRES_IMAGE = "postgres:17-alpine"


def alembic_env(pg: PostgresContainer) -> dict[str, str]:
    return {
        "LOOM_DB_HOST": pg.get_container_host_ip(),
        "LOOM_DB_PORT": str(pg.get_exposed_port(5432)),
        "LOOM_DB_NAME": pg.dbname,
        "LOOM_DB_USER": pg.username,
        "LOOM_DB_PASSWORD": pg.password,
        # Container testcontainers không cấu hình TLS; mặc định "verify-full"
        # của LOOM_DB_SSLMODE nhắm cho Aiven thật, không cho container dùng một
        # lần này. Xem ghi chú ở test_user_store.py.
        "LOOM_DB_SSLMODE": "disable",
    }


def sync_url(pg: PostgresContainer) -> str:
    """psycopg2 — cho các phép soi schema đọc thẳng pg_catalog."""
    return (
        f"postgresql+psycopg2://{pg.username}:{pg.password}"
        f"@{pg.get_container_host_ip()}:{pg.get_exposed_port(5432)}/{pg.dbname}"
    )


def async_url(pg: PostgresContainer) -> str:
    """asyncpg — cùng driver mà API chạy thật, nên câu SQL mà test quyền gửi đi
    là đúng câu mà production gửi đi."""
    return (
        f"postgresql+asyncpg://{pg.username}:{pg.password}"
        f"@{pg.get_container_host_ip()}:{pg.get_exposed_port(5432)}/{pg.dbname}"
    )


def run_alembic(pg: PostgresContainer, *args: str) -> subprocess.CompletedProcess[str]:
    # S603: `args` là hằng viết tay trong chính các test này, không phải input ngoài.
    return subprocess.run(  # noqa: S603
        ["uv", "run", "alembic", *args],  # noqa: S607 — uv chạy qua PATH có chủ đích
        cwd=API_DIR,
        env={**os.environ, **alembic_env(pg)},
        capture_output=True,
        text=True,
        check=False,
    )
