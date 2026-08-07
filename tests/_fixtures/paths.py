"""Centralized test sys.path setup that was copy-pasted across conftests.

``ensure_custom_hosts_on_path`` makes the repo's shared ``custom_hosts`` package
importable (the third-party-style frame package SUT repos depend on).
``default_sut_dir`` names the ``repo1`` fixture SUT; the integration tree's
session fixture points ``OTTO_SUT_DIRS`` at it — at RUNTIME, never at conftest
import time (G11: module-scope env writes are banned; the old
``ensure_sut_dirs()`` here was the live offender, re-injecting the variable
after the root conftest's hermeticity strip).
"""

from __future__ import annotations

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
