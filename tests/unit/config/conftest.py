"""Fixtures shared by the config unit tests."""

import pytest

from otto.config.user_settings import clear_user_settings_memo, user_settings_path


def turn_cache_off():
    """Write ``[cache] enabled = false`` to the user settings file and drop the memo."""
    path = user_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[cache]\nenabled = false\n")  # scaffold-exempt: user-level ~/.otto file
    clear_user_settings_memo()


@pytest.fixture
def cache_off():
    """Turn otto's cache off through the user settings file.

    The root conftest's autouse fixture clears the memo and unlinks the file
    around every test, so there is no teardown here.
    """
    turn_cache_off()
