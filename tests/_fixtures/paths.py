"""Centralized test path anchors and sys.path setup.

``TESTS_ROOT`` / ``PROJECT_ROOT`` are THE path anchors for test code — G9 bans
``Path(__file__).parents[N]`` arithmetic everywhere else under ``tests/``,
because depth arithmetic breaks silently when a file moves; this module is the
one sanctioned place that derives them (from its own known location).
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
TESTS_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = TESTS_ROOT.parent
_TESTS_ROOT = TESTS_ROOT
_CUSTOM_HOSTS = _TESTS_ROOT / "custom_hosts"
_REPO1 = _TESTS_ROOT / "repo1"
WHEELS_DIR = TESTS_ROOT / "_fixtures" / "wheels"
"""Committed fixture wheels (see that directory's README).

Holds ``beetroot``, the requirement that CANNOT be satisfied from an index --
so a test that installs it without pointing here has reached the network.
"""


def ensure_custom_hosts_on_path() -> None:
    """Prepend the shared ``custom_hosts`` dir to ``sys.path`` (idempotent)."""
    p = str(_CUSTOM_HOSTS)
    if p not in sys.path:
        sys.path.insert(0, p)


def default_sut_dir() -> str:
    """Path to the ``repo1`` fixture SUT used by the cov/integration suites."""
    return str(_REPO1)
