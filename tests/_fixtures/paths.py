"""Centralized test sys.path / env setup that was copy-pasted across conftests.

``ensure_custom_hosts_on_path`` makes the repo's shared ``custom_hosts`` package
importable (the third-party-style frame package SUT repos depend on).
``ensure_sut_dirs`` points OTTO_SUT_DIRS at the ``repo1`` fixture SUT -- both must
run before any ``otto`` import, so call them at conftest import time.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# tests/  (this file lives at tests/_fixtures/paths.py)
_TESTS_ROOT = Path(__file__).resolve().parents[1]
_CUSTOM_HOSTS = _TESTS_ROOT / "custom_hosts"
_REPO1 = _TESTS_ROOT / "repo1"


def ensure_custom_hosts_on_path() -> None:
    """Prepend the shared ``custom_hosts`` dir to ``sys.path`` (idempotent)."""
    p = str(_CUSTOM_HOSTS)
    if p not in sys.path:
        sys.path.insert(0, p)


def default_sut_dir() -> str:
    """Path to the ``repo1`` fixture SUT used by the cov/integration suites."""
    return str(_REPO1)


def ensure_sut_dirs() -> None:
    """Default ``OTTO_SUT_DIRS`` to ``repo1`` if unset (must precede otto import)."""
    os.environ.setdefault("OTTO_SUT_DIRS", default_sut_dir())
