"""Snapshot one environment variable and put back its exact prior state, absence included.

The one core every "this test must not leak a variable" guard uses (#785):
``monkeypatch.delenv`` records nothing for a variable that was absent, so its
teardown cannot remove a value code under test set meanwhile, and a hand-written
snapshot-and-restore is easy to get half right. ``TZ`` needs one more step, a
``time.tzset()`` after every change, which :func:`preserved_timezone` and
:func:`set_timezone` add over the same core.
"""

import contextlib
import os
import time
from collections.abc import Iterator


@contextlib.contextmanager
def preserved_env_var(name: str, *, absent_during: bool = False) -> Iterator[None]:
    """Restore *name* to exactly what it was on entry, set or absent, whatever the block did.

    With *absent_during*, *name* is removed for the duration of the block.
    """
    saved = os.environ.get(name)
    if absent_during:
        os.environ.pop(name, None)
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = saved


@contextlib.contextmanager
def preserved_timezone() -> Iterator[None]:
    """:func:`preserved_env_var` for ``TZ``, re-running ``time.tzset()`` after the restore.

    Without the ``tzset()`` the process keeps converting in the zone the block
    set, and the zone leaks into later tests.
    """
    try:
        with preserved_env_var("TZ"):
            yield
    finally:
        time.tzset()


def set_timezone(name: str) -> None:
    """Set ``TZ`` to *name* and apply it with ``time.tzset()``."""
    os.environ["TZ"] = name
    time.tzset()
