"""Fixture dùng chung cho toàn workspace."""

from collections.abc import Iterator

import pytest

from loom_core.config import get_settings


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Iterator[None]:
    """get_settings() là singleton lru_cache — xoá quanh mỗi test để tránh rò rỉ trạng thái."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
