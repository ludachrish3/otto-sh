"""Shared helper: turn otto's cache on or off through the user settings file.

The one writer of ``[cache] enabled`` for tests that need the switch set, not
tests whose subject is the file itself (the reader, ``otto cache
enable``/``disable``'s edits). The root conftest's autouse fixture clears the
memo and unlinks the file around every test, so there is no teardown.
"""

from otto.config.user_settings import clear_user_settings_memo, user_settings_path


def set_cache_enabled(on: bool) -> None:
    """Write ``[cache] enabled = <on>`` to this process's user settings file and drop the memo."""
    path = user_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"[cache]\nenabled = {'true' if on else 'false'}\n")
    clear_user_settings_memo()
