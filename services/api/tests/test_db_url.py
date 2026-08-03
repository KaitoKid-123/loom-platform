from loom_api.db import build_sqlalchemy_url
from loom_core.config import Settings


def test_url_is_built_from_parts() -> None:
    settings = Settings(
        db_host="pg", db_port=5432, db_name="loom", db_user="loom", db_password="pw"
    )
    assert build_sqlalchemy_url(settings) == "postgresql+asyncpg://loom:pw@pg:5432/loom"


def test_special_characters_in_password_are_escaped() -> None:
    settings = Settings(db_password="p@ss/w:rd")
    url = build_sqlalchemy_url(settings)
    assert "p%40ss%2Fw%3Ard" in url
    assert "p@ss/w:rd" not in url


def test_explicit_database_url_wins() -> None:
    settings = Settings(database_url="postgresql+asyncpg://x:y@z:1/db", db_host="ignored")
    assert build_sqlalchemy_url(settings) == "postgresql+asyncpg://x:y@z:1/db"
