"""Fixtures shared by the config unit tests."""

import pytest

from tests._fixtures.user_settings import set_cache_enabled


@pytest.fixture
def cache_off():
    """Turn otto's cache off through the user settings file.

    The root conftest's autouse fixture clears the memo and unlinks the file
    around every test, so there is no teardown here.
    """
    set_cache_enabled(False)
