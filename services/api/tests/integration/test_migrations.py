import os
import subprocess
from pathlib import Path

import pytest
import sqlalchemy as sa
from testcontainers.postgres import PostgresContainer

API_DIR = Path(__file__).resolve().parents[2]


@pytest.mark.integration
def test_migration_creates_schema_and_seeds_tenant() -> None:
    with PostgresContainer("postgres:17-alpine") as pg:
        env = {
            "LOOM_DB_HOST": pg.get_container_host_ip(),
            "LOOM_DB_PORT": str(pg.get_exposed_port(5432)),
            "LOOM_DB_NAME": pg.dbname,
            "LOOM_DB_USER": pg.username,
            "LOOM_DB_PASSWORD": pg.password,
        }
        result = subprocess.run(
            ["uv", "run", "alembic", "upgrade", "head"],  # noqa: S607 — uv chạy qua PATH có chủ đích
            cwd=API_DIR,
            env={**os.environ, **env},
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr

        sync_url = (
            f"postgresql+psycopg2://{pg.username}:{pg.password}"
            f"@{pg.get_container_host_ip()}:{pg.get_exposed_port(5432)}/{pg.dbname}"
        )
        engine = sa.create_engine(sync_url)
        with engine.connect() as conn:
            tables = set(sa.inspect(conn).get_table_names())
            assert {"tenant", "app_user", "user_session", "alembic_version"} <= tables
            count = conn.execute(sa.text("SELECT count(*) FROM tenant")).scalar_one()
            assert count == 1
        engine.dispose()
