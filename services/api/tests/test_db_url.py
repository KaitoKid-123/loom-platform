from loom_api.db import build_sqlalchemy_url
from loom_core.config import Settings


def test_url_is_built_from_parts() -> None:
    settings = Settings(
        db_host="pg", db_port=5432, db_name="loom", db_user="loom", db_password="pw"
    )
    assert (
        build_sqlalchemy_url(settings)
        == "postgresql+asyncpg://loom:pw@pg:5432/loom?ssl=verify-full"
    )


def test_special_characters_in_password_are_escaped() -> None:
    settings = Settings(db_password="p@ss/w:rd")
    url = build_sqlalchemy_url(settings)
    assert "p%40ss%2Fw%3Ard" in url
    assert "p@ss/w:rd" not in url


def test_explicit_database_url_wins() -> None:
    settings = Settings(database_url="postgresql+asyncpg://x:y@z:1/db", db_host="ignored")
    assert build_sqlalchemy_url(settings) == "postgresql+asyncpg://x:y@z:1/db"


def test_aiven_style_sslmode_is_translated() -> None:
    settings = Settings(database_url="postgresql+asyncpg://u:p@h:5432/db?sslmode=verify-full")
    url = build_sqlalchemy_url(settings)
    assert "ssl=verify-full" in url
    assert "sslmode" not in url


def test_other_query_params_survive_translation() -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://u:p@h:5432/db?sslmode=require&application_name=loom"
    )
    url = build_sqlalchemy_url(settings)
    assert "ssl=require" in url
    assert "application_name=loom" in url


def test_url_without_sslmode_is_untouched() -> None:
    settings = Settings(database_url="postgresql+asyncpg://u:p@h:5432/db")
    assert build_sqlalchemy_url(settings) == "postgresql+asyncpg://u:p@h:5432/db"
