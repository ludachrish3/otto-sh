"""Shared fixture: a fake `<hash8>-<slug>` workspace directory for cache tests.

Used by both ``tests/unit/config/test_cache_maintenance.py`` (the pure
maintenance layer) and ``tests/unit/cli/test_cache_cli.py`` (the ``otto
cache`` CLI surface over it) -- lives here rather than in either test module
because a helper shared ACROSS test directories is exactly what
``tests/_fixtures/`` is for; a same-directory or cross-module-but-same-tree
import (like ``test_completion_cache_unit.py`` importing a sibling in
``tests/unit/config/``) is a different, narrower case this one is not.
"""

import os
import time
from pathlib import Path

OLD = 61 * 86400
YOUNG = 59 * 86400


def _mk_workspace(
    home: Path,
    name: str,
    *,
    cache_age: float | None = OLD,
    sidecar: bool = False,
    sidecar_age: float | None = None,
    env: bool = False,
) -> Path:
    ws = home / name
    ws.mkdir(parents=True)
    now = time.time()
    if cache_age is not None:
        f = ws / "completion_cache.json"
        f.write_text("{}")
        os.utime(f, (now - cache_age, now - cache_age))
    if sidecar:
        s = ws / "remote_completion_cache.json"
        s.write_text("{}")
        age = OLD if sidecar_age is None else sidecar_age
        os.utime(s, (now - age, now - age))
    if env:
        (ws / "env" / "bin").mkdir(parents=True)
        (ws / "env" / "bin" / "python").write_text("")
    return ws
